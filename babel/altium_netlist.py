"""The compiled netlist of an exported Altium project, corrected.

Both the board exporter (which needs the names Altium will give the nets) and
the project verifier (which needs the partition) compile the project we just
wrote. They must correct the compiler the SAME way or they judge different
designs — hence one home for it here.

The correction: altium_monkey's compiler drops the sheet-entry <-> port weld
under a STRICT hierarchical scope as soon as the parent net also carries a
power port. In Altium an explicit entry-to-port connection stands whatever
the net identifier scope is — the scope governs NAMES, not wires. So the
weld is re-applied from the compiler's own record of the hierarchy
(`schematic_hierarchy['links']`). Without it a module port that meets a
supply symbol on the parent sheet reads as disconnected: on step4 that is
VIN, GND and VDD_5V of all four channels, while EN — whose parent net is
spelled by a plain net label — comes through.

Also folded in here: two nets of ONE sheet spelled with the same name are
one net in Altium whatever kind of identifier spells it (power port, port,
label); altium_monkey keeps them apart.
"""
from altium_monkey import AltiumDesign


def _sheet_key(endpoint):
    """Which SHEET INSTANCE an endpoint sits on. Not the file name: one child
    document is a separate sheet per channel and its local names repeat in
    every one of them."""
    idx = getattr(endpoint, 'compiled_sheet_index', None)
    return idx if idx is not None else endpoint.source_sheet


def _identifier_names(net):
    """(sheet, name) pairs this net is spelled by."""
    out = set()
    for e in net.endpoints:
        if e.role in ('power_port', 'port') and e.name:
            out.add((_sheet_key(e), e.name))
    if not net.auto_named and net.name:
        for e in net.endpoints:
            out.add((_sheet_key(e), net.name))
    return out


def compiled_nets(prj_path):
    """([(net name, {(designator, pin)})], unresolved links).

    Pin keys are raw — the caller decides how to spell a pin, because the
    board knows pads and the schematic knows pin designators.
    """
    netlist = AltiumDesign.from_prjpcb(str(prj_path)).to_netlist()

    pins, name, auto, net_of_object = {}, {}, {}, {}
    for net in netlist.nets:
        pins[net.uid] = {(e.designator, e.pin or e.pin_name)
                         for e in net.endpoints if e.role == 'pin'}
        name[net.uid] = net.name
        auto[net.uid] = net.auto_named
        for e in net.endpoints:
            if e.role in ('sheet_entry', 'port') and e.element_id:
                # A port object belongs to the SOURCE document, so its id
                # repeats in every channel — the sheet instance is part of
                # the key.
                net_of_object[(getattr(e, 'compiled_sheet_index', None),
                               e.element_id)] = net.uid

    # Union-find: a net can be welded twice (two entries of one sheet symbol
    # landing on the same parent net), and a pop-based merge would silently
    # drop the second weld.
    uf = {}

    def root(u):
        while uf.setdefault(u, u) != u:
            uf[u] = uf[uf[u]]
            u = uf[u]
        return u

    def merge(a, b):
        if a is None or b is None:
            return
        ra, rb = root(a), root(b)
        if ra == rb:
            return
        uf[rb] = ra
        pins.setdefault(ra, set()).update(pins.pop(rb, set()))
        # The surviving name is the one an engineer wrote: a parent's label
        # beats a channel's auto-generated NetC1_DCDC1_2.
        if auto.get(ra, True) and not auto.get(rb, True):
            name[ra], auto[ra] = name[rb], False

    same_sheet = {}
    for net in netlist.nets:
        for key in _identifier_names(net):
            same_sheet.setdefault(key, []).append(net.uid)
    for uids in same_sheet.values():
        for other in uids[1:]:
            merge(uids[0], other)

    hierarchy = netlist.schematic_hierarchy
    for link in hierarchy.get('links', []):
        par, ch = link.get('parent', {}), link.get('child', {})
        parent_net = net_of_object.get((par.get('compiled_sheet_index'),
                                        par.get('object_id')))
        for oid in ch.get('object_ids', []):
            merge(parent_net,
                  net_of_object.get((ch.get('compiled_sheet_index'), oid)))

    return ([(name[r], pins[r]) for r in pins],
            hierarchy.get('unresolved', []))
