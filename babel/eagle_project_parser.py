"""Eagle SCHEMATIC (.sch) -> IR project (.swprj).

Eagle-side analogue of kicad_project_parser.py, but structurally simpler in
one key respect: Eagle's own <segment> element already groups exactly what
belongs to one electrically-connected island (<pinref>/<portref>/<wire>/
<junction>/<label> children, eagle.dtd: "segment (pinref | portref | wire |
junction | label)*") — decisions.md "Junction и pinref — хранятся ЯВНО, не
выводятся из совпадения координат" confirms this is deliberate Eagle format
design, not a renderer cache. Unlike KiCad (whose raw wire geometry needs
kicad_schematic.py's union-find to INFER connectivity), this importer reads
the existing structure close to 1:1 — no _build_nets/_DSU equivalent here.

A `.sch` is self-contained (embedded <schematic><libraries><library> carries
every symbol/package/deviceset the file's <parts> reference) — no external
library-resolution table to consult, unlike KiCad's sym-lib-table precondition.
eagle_parser.py's convert_symbol/convert_package/convert_deviceset/
collect_attributes are reused verbatim against this embedded library, since
its shape is identical to a standalone .lbr's <library>.

Stray/page-membership validation (ir_schema.md's frame-boundary concept,
mirroring kicad_project_parser's page/frame handling): per the user, Eagle
best practice is exactly one <frame> per <sheet> (needed for correct
printing) even though eagle.dtd permits 0 or many. This importer hard-fails
on both non-standard cases, with a distinct message each:
  - 0 frames on a sheet: tell the user to add one.
  - 2+ frames on a sheet: tell the user to keep only one.
Multiple <sheet> elements (each carrying exactly one frame, by the rule
above) are tiled horizontally into one IR canvas — the same convention
kicad_project_parser._detect_pages/convert_project_full uses for KiCad's
flat multi-page projects.
"""
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from babel.eagle_parser import (
    convert_symbol, convert_package, convert_deviceset, collect_attributes,
    parse_rot, _um,
)
from babel.eagle_exporter import _rotate_vec, _SIDE_NORMAL, _port_geometry, _PORT_PIN_LEN_UM
from babel.ir_util import component_gates, is_multi_gate
from babel import import_log

_GAP_UM = 12700  # 0.5" gap between tiled pages/sheets, same as kicad_project_parser


# ---------------------------------------------------------------------------
# Step 2: embedded <libraries> -> project pool (reuses eagle_parser as-is)
# ---------------------------------------------------------------------------

def _convert_embedded_libraries(schematic_el, symbols_el, proj_el, pool, src_stem):
    """Walk every <schematic><libraries><library> (there is exactly one in
    every ground-truth fixture, but eagle.dtd allows (library)* so this
    iterates defensively), reusing eagle_parser's own symbol/package/
    deviceset conversion unchanged — an embedded <library> has the exact
    same <packages>/<symbols>/<devicesets> shape as a standalone .lbr's
    <library> (confirmed against testData/min.sch and outputs/multichannel.sch).

    Returns {library_name: {comp_name: <component> element}} — needed by
    the <parts> resolution step to know which library a given deviceset
    name came from (Eagle designs commonly embed only one, but the DTD
    doesn't forbid several).
    """
    libs_el = schematic_el.find('libraries')
    comps_by_lib = {}
    if libs_el is None:
        return comps_by_lib

    def _canon(el, skip=()):
        import copy as _copy
        c = _copy.deepcopy(el)
        for k in skip:
            c.attrib.pop(k, None)
        return ET.tostring(c, encoding='unicode')

    def _unique(base, taken):
        n = 1
        while f'{base}@{n}' in taken:
            n += 1
        return f'{base}@{n}'

    # Same-NICKNAME collisions are real (PowerPCB: six duplicate pairs —
    # urn-pinned managed library + a plain edited copy, both "mosfet", with
    # DIVERGED content: 2N7002 on SOT23 in one, SOT23-3 in the other; the
    # old silent "first wins" dedup bound Q16 to the wrong package).
    # Identical twins still dedup; diverged twins get Eagle's own "@N"
    # suffix convention — on the library nickname, the component name and
    # the symbol name alike, never silently.
    lib_nicknames_taken = set()

    for lib_el in libs_el.findall('library'):
        lib_name = lib_el.get('name') or src_stem
        lib_urn = lib_el.get('urn', '')
        if lib_name in lib_nicknames_taken:
            eff_lib_name = _unique(lib_name, lib_nicknames_taken)
            import_log.log(lib_name, lib_urn or '(no urn)',
                           'LIBRARY nickname collision, renamed to', eff_lib_name)
        else:
            eff_lib_name = lib_name
        lib_nicknames_taken.add(eff_lib_name)
        sym_renames = {}
        packages_el = lib_el.find('packages')
        packages = {p.get('name'): p for p in packages_el.findall('package')} \
            if packages_el is not None else {}

        symbols_container = lib_el.find('symbols')
        if symbols_container is not None:
            for sym_el in symbols_container.findall('symbol'):
                sym_name = sym_el.get('name')
                converted = convert_symbol(sym_el, sym_name)
                if sym_name in pool:
                    if _canon(pool[sym_name]) == _canon(converted):
                        continue   # identical twin (shared across sheets/modules)
                    new_name = _unique(sym_name, pool)
                    converted.set('name', new_name)
                    sym_renames[sym_name] = new_name
                    import_log.log(eff_lib_name, sym_name,
                                   'SYMBOL name collision (diverged content), '
                                   'renamed to', new_name)
                    sym_name = new_name
                symbols_el.append(converted)
                pool[sym_name] = converted

        comps = {}
        used_pkg_names = set()
        devicesets_el = lib_el.find('devicesets')
        if devicesets_el is not None:
            for ds_el in devicesets_el.findall('deviceset'):
                # One deviceset -> N components, one per distinct Eagle
                # <technology> name (convert_deviceset — ir_schema.md has no
                # equivalent third level, so each real technology variant
                # becomes its own independent component, e.g. "R"/"R-1%"/
                # "R-5%"). Indexed here by (deviceset_name, tech_name) so
                # _collect_sheet can resolve a <part deviceset= technology=>
                # to the exact component it actually names.
                ds_name = ds_el.get('name')
                for comp_el in convert_deviceset(ds_el, packages):
                    # this library's diverged-symbol renames apply to the
                    # component's gate references before any comparison
                    if sym_renames:
                        if comp_el.get('symbol') in sym_renames:
                            comp_el.set('symbol', sym_renames[comp_el.get('symbol')])
                        for g in comp_el.findall('gate'):
                            if g.get('symbol') in sym_renames:
                                g.set('symbol', sym_renames[g.get('symbol')])
                    comp_name = comp_el.get('name')
                    tech_name = comp_name[len(ds_name):] if comp_name.startswith(ds_name) else ''
                    existing = proj_el.find(f'component[@name="{comp_name}"]')
                    if existing is not None and \
                            _canon(existing, skip=('library',)) != _canon(comp_el, skip=('library',)):
                        new_name = _unique(comp_name,
                                           {c.get('name') for c in proj_el.findall('component')})
                        comp_el.set('name', new_name)
                        # flat-pool uniqueness only; the EAGLE-facing name
                        # (deviceset naming, implicit-value derivation) stays
                        # the original — same-named devicesets in different
                        # libraries are legal Eagle, and the exporter already
                        # separates the libraries (mosfet / mosfet@1)
                        comp_el.set('renamed-from', comp_name)
                        import_log.log(eff_lib_name, comp_name,
                                       'COMPONENT name collision (diverged content), '
                                       'renamed to', new_name)
                        comp_name = new_name
                        existing = None
                    if existing is None and proj_el.find(f'component[@name="{comp_name}"]') is None:
                        # `library=` — the component's ORIGIN library
                        # nickname, not a live reference (same "copy, not
                        # link" discipline ir_schema.md "Component instance"
                        # already uses for placement — see decisions.md
                        # "Component instance на схеме"). Eagle always has a
                        # real, meaningful library name here (unlike KiCad,
                        # which has no equivalent per-component concept
                        # worth reading on that side) — per the user: keep
                        # it, don't collapse every component into one
                        # library on export the way the KiCad path is
                        # forced to (KiCad has no notion of library ORDER/
                        # grouping worth preserving; Eagle's is real and
                        # used).
                        comp_el.set('library', eff_lib_name)
                        proj_el.append(comp_el)
                    comps[(ds_name, tech_name)] = proj_el.find(f'component[@name="{comp_name}"]')
                devices_el = ds_el.find('devices')
                if devices_el is not None:
                    for device in devices_el.findall('device'):
                        pkg = device.get('package', '')
                        if pkg:
                            used_pkg_names.add(pkg)
        # Keyed by (name, urn), not bare name: Eagle can embed two distinct
        # libraries under the SAME nickname (seen in testData/modtest.sch —
        # a urn-pinned "frames" library and a separately-edited plain
        # "frames" both present; Eagle itself disambiguates via a "@N" name
        # suffix on ONE of them but not always, so bare-name collision is
        # real). `library_urn` on <part> is the actual disambiguator when
        # present; absent on both sides (urn='') still collides, same as
        # real Eagle project files where a bare nickname is genuinely unique.
        comps_by_lib[(lib_name, lib_urn)] = comps

        # Orphaned packages (defined but never referenced by any deviceset)
        # have no IR home at the <project> level (unlike a .swlib's
        # <library>, <project> only permits <component>/<module>/
        # <schematic>/<classes>/<symbols> — see kicad_project_parser's own
        # <project> shape) — log and drop, same "honest degradation,
        # logged" discipline used throughout this codebase.
        for pkg_name in packages:
            if pkg_name not in used_pkg_names:
                import_log.log(pkg_name, lib_name,
                                'ORPHAN_PACKAGE not referenced by any deviceset, dropped '
                                '(.swprj has no top-level bare-footprint slot)')

    return comps_by_lib


# ---------------------------------------------------------------------------
# Step 3: <parts> -> designator -> (deviceset, device, value, library) table
# ---------------------------------------------------------------------------

def _collect_parts(container_el):
    """<parts><part name= library= deviceset= device= value= technology=?>
    <attribute name= value=/>*</part></parts> -> {part_name: {...}}.

    `technology` (e.g. "-1%"/"-5%" tolerance) selects which of
    convert_deviceset's technology-split components this part actually
    uses (see that function and _convert_embedded_libraries — one Eagle
    deviceset becomes N independent IR components, one per distinct
    technology name, since IR has no third level below component/footprint
    to hold Eagle's device->technology axis). Kept here as a real,
    consumed field (not just logged) — resolution happens where the
    component is looked up (comp_by_name keyed by (library, deviceset,
    technology)).

    A <part>'s own <attribute name= value=/> children (eagle.dtd: `part
    (attribute*, variant*)`) are a PER-INSTANCE override, distinct from the
    device/technology-level attributes already carried on the <component>
    (ir_schema.md "Component instance": generic per-placement facts like a
    real MANF#/LCSC# for one specific resistor, not a property of the
    generic "R" deviceset itself) — same role IR's own <instance><attr>
    already gives `value`. Found missing on testData/maximus.sch's own
    round-trip (user caught it): most passive components (R1/R3/... "rc"
    library) carry real per-designator MANF#/LCSC#/ALLOCATED attributes
    directly on their schematic-level <part>, entirely separate from the
    <instance><attribute> children (which only carry PLACEHOLDER GEOMETRY —
    x/y/size/layer for >NAME/>VALUE/etc., no `value=` of their own) already
    read elsewhere. Distinguished structurally: an <attribute> with `value=`
    present is a real per-instance fact; one without (geometry-only,
    <instance> context) is not — matches eagle.dtd's own comment ("display:
    only in <element> or <instance> context") confirming these are two
    different attribute USES sharing one element name, not the same thing.
    """
    parts = {}
    parts_el = container_el.find('parts')
    if parts_el is None:
        return parts
    for part_el in parts_el.findall('part'):
        name = part_el.get('name')
        attrs = [(a.get('name').lower(), a.get('value'))
                 for a in part_el.findall('attribute') if a.get('value') is not None]
        parts[name] = {
            'library': part_el.get('library'),
            'library_urn': part_el.get('library_urn', ''),
            'deviceset': part_el.get('deviceset'),
            'technology': part_el.get('technology', ''),
            'device': part_el.get('device', ''),
            'value': part_el.get('value'),
            'attrs': attrs,
        }
    return parts


# ---------------------------------------------------------------------------
# Step 4: placement math — instance rot/mirror -> absolute position
# ---------------------------------------------------------------------------

def _abs_pos_um(local_x_um, local_y_um, inst_x_um, inst_y_um, angle_deg, mirror):
    """Local (symbol-space, Y-up) point -> absolute IR-canvas position, given
    an instance's Eagle rot="[M]R{angle}" attribute (parsed by
    eagle_parser.parse_rot). No Y-flip compensation anywhere — Eagle's own
    coordinate convention is already Y-up, matching IR canon exactly (unlike
    KiCad, whose Y-down sheet space needs the flip kicad_schematic.
    _abs_pin_pos_mm removes — see that function's docstring for the
    long-form derivation; this is the same rotate+mirror composition, ported
    without the Y-flip KiCad needed and Eagle never did).
    """
    rx, ry = _rotate_vec(local_x_um, local_y_um, angle_deg, mirror)
    return inst_x_um + round(rx), inst_y_um + round(ry)


# ---------------------------------------------------------------------------
# Step 6: canvas content — <instances>/<nets>/<plain> -> IR instance/net dicts
# ---------------------------------------------------------------------------

def _pin_positions(sym_el, gate_x_um, gate_y_um, inst_x_um, inst_y_um, angle_deg, mirror):
    """{pin_name: (abs_x_um, abs_y_um)} for every <pin> of one gate's symbol,
    composing TWO placements: the gate's own offset within the deviceset
    (gate_x/gate_y, itself in the symbol's local frame, always rot=0/mirror=0
    per eagle.dtd <gate> — no rot/mirror attribute exists on <gate>) and the
    instance's placement rot/mirror on the sheet.
    """
    out = {}
    for pin_el in sym_el.findall('pin'):
        pin_deg, pin_mirror = parse_rot(pin_el.get('rot'))
        # A pin's own local rot only affects which direction its stub draws
        # (not needed here — we want the pin's ORIGIN, which parse_rot's
        # angle doesn't move) — the origin is gate offset + pin x/y, then
        # the WHOLE gate rotates/mirrors with the instance.
        lx = float(pin_el.get('x', '0')) * 1000 + gate_x_um
        ly = float(pin_el.get('y', '0')) * 1000 + gate_y_um
        ax, ay = _abs_pos_um(lx, ly, inst_x_um, inst_y_um, angle_deg, mirror)
        out[pin_el.get('name')] = (ax, ay)
    return out


def _trim_portref_wire(seg, port_geom, part_name, port_name):
    """A real Eagle-drawn wire to a module port stops at the PIN's far end
    (0.2" = 5.08mm outside the block perimeter, Eagle's own on-canvas pin
    length — eagle_exporter._PORT_PIN_LEN_UM), never at the bare perimeter.
    IR canon is the opposite (ir_schema.md "Модуль": the port's connection
    point on the parent IS the perimeter position, no stub — same
    convention kicad_project_parser already produces, since KiCad attaches
    wires directly at the sheet edge) — eagle_exporter._emit_segment's own
    trim/bridge logic re-adds this exact stub symmetrically on export. Without
    this import-side counterpart the stub survives into IR and then into a
    KiCad export as a wire stopping 5.08mm short of the sheet pin (found on
    testData/modtest.sch's own round-trip — user caught it). Mutates the one
    wire (if any) whose end sits exactly on the pin's attachment point,
    trimming it back onto the perimeter; a segment with no matching wire
    (already at the perimeter, or none drawn at all) is untouched.
    """
    g = port_geom.get((part_name, port_name))
    if g is None:
        return
    perim_x, perim_y, nx, ny = g
    target = (perim_x + nx * _PORT_PIN_LEN_UM, perim_y + ny * _PORT_PIN_LEN_UM)
    perim_x_s, perim_y_s = str(round(perim_x)), str(round(perim_y))
    for i, (x1, y1, x2, y2, w) in enumerate(seg['wires']):
        if abs(float(x1) - target[0]) < 0.5 and abs(float(y1) - target[1]) < 0.5:
            seg['wires'][i] = (perim_x_s, perim_y_s, x2, y2, w)
            return
        if abs(float(x2) - target[0]) < 0.5 and abs(float(y2) - target[1]) < 0.5:
            seg['wires'][i] = (x1, y1, perim_x_s, perim_y_s, w)
            return


def _collect_sheet(sheet_el, parts, pool, comp_by_name, comps_by_lib, ctx, port_geom=None):
    """One <sheet> -> accumulator dict: instances (list of dicts, IR-ready),
    nets (list of {'name', 'class', 'segments': [...]}). Straight structural
    read — Eagle's <segment> already IS the electrical island, no inference.
    """
    instances = []
    seen_designator_gate = set()   # (designator,) -> True once value attr written

    instances_el = sheet_el.find('instances')
    if instances_el is not None:
        for inst_el in instances_el.findall('instance'):
            part_name = inst_el.get('part')
            part = parts.get(part_name)
            if part is None:
                raise ValueError(f'<instance part="{part_name}"> references a <part> '
                                  f'that does not exist in <parts>.')
            comp_el = comp_by_name.get((part['library'], part['library_urn'],
                                        part['deviceset'], part['technology']))
            if comp_el is None:
                raise ValueError(
                    f'part "{part_name}": deviceset "{part["deviceset"]}" (technology '
                    f'"{part["technology"]}") in library "{part["library"]}" not found among '
                    f'the schematic\'s embedded libraries.')
            multi = is_multi_gate(comp_el)
            gate_name = inst_el.get('gate', 'G$1')

            angle_deg, mirror = parse_rot(inst_el.get('rot'))
            x_um, y_um = _um(inst_el.get('x')), _um(inst_el.get('y'))

            inst = {
                'component': comp_el.get('name'), 'library': part_name and part['library'],
                'designator': part_name, 'x': x_um, 'y': y_um,
                'rot': str(round(angle_deg)) if angle_deg else '0',
                'mirror': '1' if mirror else '0',
                'attrs': [],
            }
            if multi:
                inst['gate'] = gate_name

            # ir_schema.md "Component instance": `footprint` is only
            # recorded when the component has 2+ (matches
            # eagle_exporter._emit_part's own comment on the reverse
            # direction) — Eagle's <part device="..."> selects exactly this
            # variant. IR's own <instance footprint=...> must reference the
            # footprint's NAME (eagle_exporter.dev_name_by_fp looks up by
            # `fp.get('name')`, not `variant`) — convert_deviceset stores the
            # device variant string on <footprint variant="...">, keyed by
            # PACKAGE name (e.g. name="R0603" variant="-0603"), so resolve
            # the device string back to its owning footprint's name here.
            # An empty device="" (a legitimate Eagle device variant with no
            # suffix — testData/maximus.sch "NSIP83086(V)", one device named
            # "-SO-20W", another named "") means convert_deviceset never set
            # `variant` on that footprint at all (only sets it `if
            # dev_variant`), so the lookup key there is None, not "" —
            # handled explicitly, not just "falls through" (an empty string
            # is also falsy, which would otherwise silently skip recording
            # `footprint` altogether for this very case, defeating the whole
            # point of this block).
            fp_variants = comp_el.findall('footprint')
            if len(fp_variants) > 1:
                device = part.get('device') or None
                fp_by_variant = {fp.get('variant'): fp.get('name') for fp in fp_variants}
                fp_name = fp_by_variant.get(device)
                if fp_name:
                    # Package NAME is NOT a unique device identity: one
                    # package can back several devices (luminoso CON-2P:
                    # devices -B2B-XH and -DS1069M both use B2B-XH-A; ERC
                    # caught the wrong device coming back). When ambiguous,
                    # record the VARIANT string — unique by construction.
                    names = [fp.get('name') for fp in fp_variants]
                    if names.count(fp_name) > 1:
                        inst['footprint'] = device
                    else:
                        inst['footprint'] = fp_name
                else:
                    import_log.log(part_name, part.get('device', ''),
                                    'DEVICE_VARIANT not found among component footprints, '
                                    'instance footprint left unresolved')

            if part_name not in seen_designator_gate:
                seen_designator_gate.add(part_name)
                if part.get('value'):
                    inst['attrs'].append(('value', part['value']))
                inst['attrs'].extend(part.get('attrs', []))
            instances.append(inst)

    nets = []
    nets_el = sheet_el.find('nets')
    if nets_el is not None:
        for net_el in nets_el.findall('net'):
            class_num = net_el.get('class', '0')
            class_name = ctx['class_name_by_num'].get(class_num)
            net = {'name': net_el.get('name'), 'class': class_name, 'segments': []}
            for seg_el in net_el.findall('segment'):
                seg = {'pinrefs': [], 'wires': [], 'junctions': [], 'labels': []}
                for p in seg_el.findall('pinref'):
                    part_name = p.get('part')
                    part = parts.get(part_name)
                    comp_el = comp_by_name.get(
                        (part['library'], part['library_urn'],
                         part['deviceset'], part['technology'])) if part else None
                    pin_name = p.get('pin')
                    if comp_el is not None and is_multi_gate(comp_el):
                        pin_name = f"{p.get('gate')}.{pin_name}"
                    seg['pinrefs'].append((part_name, pin_name))
                for p in seg_el.findall('portref'):
                    # eagle.dtd <portref moduleinst= port=> -> IR <portref
                    # part= port=> (rename moduleinst->part, matching
                    # <pinref>'s own attribute naming, ir_schema.md "Модуль").
                    seg['pinrefs'].append((p.get('moduleinst'), ('__portref__', p.get('port'))))
                for w in seg_el.findall('wire'):
                    seg['wires'].append((
                        _um(w.get('x1')), _um(w.get('y1')),
                        _um(w.get('x2')), _um(w.get('y2')),
                        _um(w.get('width', '0')),
                    ))
                if port_geom:
                    for p in seg_el.findall('portref'):
                        _trim_portref_wire(seg, port_geom, p.get('moduleinst'), p.get('port'))
                for j in seg_el.findall('junction'):
                    seg['junctions'].append((_um(j.get('x')), _um(j.get('y'))))
                for l in seg_el.findall('label'):
                    deg, _ = parse_rot(l.get('rot'))
                    seg['labels'].append((
                        _um(l.get('x')), _um(l.get('y')), _um(l.get('size', '1.778')),
                        str(round(deg)),
                    ))
                net['segments'].append(seg)
            nets.append(net)

    busses_el = sheet_el.find('busses')
    if busses_el is not None:
        for bus_el in busses_el.findall('bus'):
            if list(bus_el):
                import_log.log(bus_el.get('name', ''), '',
                                'BUS content not modeled, skipped (member signals still '
                                'appear as ordinary <net>s elsewhere)')

    return instances, nets


def _write_canvas(parent_el, instances, nets):
    """Accumulated canvas content -> IR XML children of `parent_el` — either
    <schematic> or <module>, same content model kicad_project_parser's
    _write_canvas targets (ir_schema.md "Модуль": та же структура что у
    <schematic>)."""
    for inst in instances:
        inst_el = ET.SubElement(parent_el, 'instance', component=inst['component'],
                                 library=inst['library'], name=inst['designator'],
                                 x=inst['x'], y=inst['y'], rot=inst['rot'], mirror=inst['mirror'])
        if inst.get('gate'):
            inst_el.set('gate', inst['gate'])
        if inst.get('footprint'):
            inst_el.set('footprint', inst['footprint'])
        for k, v in inst['attrs']:
            ET.SubElement(inst_el, 'attr', name=k, value=v)

    for net in nets:
        net_kwargs = {'name': net['name']}
        if net.get('class'):
            net_kwargs['class'] = net['class']
        net_el = ET.SubElement(parent_el, 'net', **net_kwargs)
        for seg in net['segments']:
            seg_el = ET.SubElement(net_el, 'segment')
            for designator, pin_name in seg['pinrefs']:
                if isinstance(pin_name, tuple) and pin_name[0] == '__portref__':
                    ET.SubElement(seg_el, 'portref', part=designator, port=pin_name[1])
                else:
                    ET.SubElement(seg_el, 'pinref', part=designator, pin=pin_name)
            for x1, y1, x2, y2, width in seg['wires']:
                ET.SubElement(seg_el, 'line', x1=x1, y1=y1, x2=x2, y2=y2, width=width)
            for jx, jy in seg['junctions']:
                ET.SubElement(seg_el, 'junction', x=jx, y=jy)
            for lx, ly, size, rot in seg['labels']:
                ET.SubElement(seg_el, 'label', x=lx, y=ly, size=size, rot=rot, style='crummy')


# ---------------------------------------------------------------------------
# Step 7: frames — stray validation (hard-reject on 0 or 2+ per sheet)
# ---------------------------------------------------------------------------

def _symbol_frame_el(sym_el):
    """A <symbol>'s own FRAME-layer <shape>, if it has one — Eagle's SECOND
    way to put a frame on a sheet: a library "Frame" deviceset (e.g. the
    stock frames.lbr, prefix commonly "FRAME" but that's just convention,
    NOT a reliable signal per the user) whose symbol carries a <frame>
    element instead of the sheet's <plain> carrying one directly. Confirmed
    real (testData/maximus.sch: <symbol name="A3L-LOC"> — no <pin>s at all,
    just <wire>/<text>/<frame> — placed on each top-level sheet as an
    ordinary <part>/<instance>, e.g. part name="FRAME1"). The only reliable
    signal is structural: does this symbol contain a FRAME-layer shape, full
    stop — not the deviceset/part name.

    `sym_el` here is the already-CONVERTED IR <symbol> (this project's own
    pool, built by eagle_parser.convert_symbol — see
    _convert_embedded_libraries) — Eagle's own <frame> element became an IR
    `<shape layer="FRAME">` there (ir_schema.md "Frame": "Ровно один
    прямоугольник на символ"), so that's what gets searched for here, not a
    raw Eagle <frame> tag.
    """
    return next((s for s in sym_el.findall('shape') if s.get('layer') == 'FRAME'), None)


def _validate_and_collect_frame(sheet_el, sheet_label, parts, comp_by_name, pool):
    """Exactly one frame per <sheet> is Eagle best practice (needed for
    correct printing) even though eagle.dtd permits 0 or many, and Eagle
    itself offers TWO independent ways to place one: (a) a native <frame>
    element directly in <plain>, or (b) an ordinary placed <instance> of a
    library "Frame" component whose SYMBOL contains a <frame> element (see
    _symbol_frame_el) — both count toward the same "how many frames on this
    sheet" total. Hard-reject both non-standard cases with a distinct,
    actionable message — 0 frames would otherwise silently manifest as a
    confusing "everything is stray" failure once page-membership validation
    runs, so it gets its own friendly message instead of that indirect
    symptom.

    Returns (frame_kind, frame_el_or_bbox_source) — either ('plain', <frame>
    element) or ('instance', (inst_el, sym_el)), whichever was found, for
    the caller to compute the page bounding box from.
    """
    plain_el = sheet_el.find('plain')
    plain_frames = plain_el.findall('frame') if plain_el is not None else []
    found = [('plain', f) for f in plain_frames]

    instances_el = sheet_el.find('instances')
    if instances_el is not None:
        for inst_el in instances_el.findall('instance'):
            part = parts.get(inst_el.get('part'))
            if part is None:
                continue
            comp_el = comp_by_name.get((part['library'], part['library_urn'],
                                        part['deviceset'], part['technology']))
            if comp_el is None:
                continue
            for _, sym_name in component_gates(comp_el):
                sym_el = pool.get(sym_name)
                if sym_el is not None and _symbol_frame_el(sym_el) is not None:
                    found.append(('instance', (inst_el, sym_el)))
                    break

    if len(found) == 0:
        raise ValueError(f'Будь приличным человеком, добавь рамку на лист {sheet_label}')
    if len(found) > 1:
        raise ValueError(f'На странице {sheet_label} найдено {len(found)} рамок. '
                          f'Сделай так, чтобы была только одна.')
    return found[0]


def _frame_bbox_um(frame_kind, frame_src):
    """Frame boundary -> (x1_um, y1_um, x2_um, y2_um), regardless of which
    of Eagle's two frame mechanisms produced it (see
    _validate_and_collect_frame)."""
    if frame_kind == 'plain':
        frame_el = frame_src
        x1, y1 = _um(frame_el.get('x1')), _um(frame_el.get('y1'))
        x2, y2 = _um(frame_el.get('x2')), _um(frame_el.get('y2'))
        return int(x1), int(y1), int(x2), int(y2)

    # 'instance': the symbol's own FRAME shape is in SYMBOL-LOCAL space
    # (Y-up, µm, center+half-extents — ir_schema.md "Frame") — the placed
    # instance may be rotated/mirrored, but a Frame symbol is never placed
    # with rot/mirror in practice (eagle_exporter._frame_bbox's own comment:
    # "Frame instances are always rot=0/mirror=0"); this importer does not
    # attempt to support a rotated frame instance — if one is ever found,
    # the plain translation below would be wrong and needs revisiting, but
    # no such fixture exists yet.
    inst_el, sym_el = frame_src
    shape_el = _symbol_frame_el(sym_el)
    ix_um, iy_um = int(_um(inst_el.get('x'))), int(_um(inst_el.get('y')))
    cx, cy = int(shape_el.get('x')), int(shape_el.get('y'))
    half_w, half_h = int(shape_el.get('w')) // 2, int(shape_el.get('h')) // 2
    x1, y1 = ix_um + cx - half_w, iy_um + cy - half_h
    x2, y2 = ix_um + cx + half_w, iy_um + cy + half_h
    return x1, y1, x2, y2


def _collect_tiled_pages(sheet_els, parts, pool, comp_by_name, comps_by_lib, ctx, page_label,
                          on_page=None):
    """N <sheet> elements -> one tiled canvas worth of (instances, nets),
    horizontally offset by each page's own frame width + _GAP_UM (same
    convention kicad_project_parser.convert_project_full uses for KiCad's
    flat multi-page projects). Shared by BOTH the top-level <schematic> and
    each <module>'s own <sheets> — a module can have more than one page too
    (eagle.dtd: module (..., sheets?), <sheets> is (sheet)*, no different
    from the top level structurally) — confirmed as a real (if rare)
    possibility by the user, even though every fixture seen so far has
    exactly one page per module. `page_label` names the sheet in stray-
    validation error messages (plain page number at top level, "module X
    page N" inside a module — see callers). `on_page(sheet_el,
    tile_x_offset)`, if given, runs once per page BEFORE that page's own
    content is collected — used by the top-level caller to convert
    <moduleinsts> (a module canvas never has these itself, module nesting
    is forbidden, so module callers simply omit this hook) — and may return
    a `port_geom` dict (see eagle_exporter._port_geometry) in the SAME
    LOCAL, pre-tile-offset coordinate space as this page's own wires, used
    to trim <portref> wire stubs back onto the port's perimeter position
    (_trim_portref_wire) before the tile offset below is applied to
    everything uniformly.

    Every sheet is required to carry EXACTLY one frame (native <frame> in
    <plain>, OR a placed component instance whose symbol carries a
    FRAME-layer shape — see _validate_and_collect_frame) — hard-fails
    otherwise. The frame COMPONENT instance itself (if that's the mechanism
    used) is kept as an ordinary <instance> in the output, same as any other
    placed part — only its bounding box is consulted here, for stray/tiling
    purposes.
    """
    instances, nets = [], []
    tile_x_offset = 0
    for idx, sheet_el in enumerate(sheet_els, start=1):
        frame_kind, frame_src = _validate_and_collect_frame(
            sheet_el, page_label(idx), parts, comp_by_name, pool)
        x1, y1, x2, y2 = _frame_bbox_um(frame_kind, frame_src)

        port_geom = None
        if on_page is not None:
            port_geom = on_page(sheet_el, tile_x_offset)

        page_instances, page_nets = _collect_sheet(sheet_el, parts, pool, comp_by_name,
                                                     comps_by_lib, ctx, port_geom=port_geom)
        for inst in page_instances:
            inst['x'] = str(int(inst['x']) + tile_x_offset)
        for net in page_nets:
            for seg in net['segments']:
                seg['wires'] = [(str(int(x1_) + tile_x_offset), y1_,
                                  str(int(x2_) + tile_x_offset), y2_, w)
                                 for x1_, y1_, x2_, y2_, w in seg['wires']]
                seg['junctions'] = [(str(int(jx) + tile_x_offset), jy)
                                     for jx, jy in seg['junctions']]
                seg['labels'] = [(str(int(lx) + tile_x_offset), ly, size, rot)
                                  for lx, ly, size, rot in seg['labels']]
        instances.extend(page_instances)
        nets.extend(page_nets)

        tile_x_offset += (x2 - x1) + _GAP_UM

    return instances, nets


# ---------------------------------------------------------------------------
# Step 9: net classes
# ---------------------------------------------------------------------------

def _collect_classes(schematic_el, proj_el):
    """<classes><class number= name= width= drill=>...</class></classes> ->
    IR <classes><class name= width= drill=>...</class></classes> +
    {number: name-or-None} lookup table (inverse of eagle_exporter's own
    class_num dict) used to resolve each <net class="N"> to an IR class
    NAME. Class "0"/"default" resolves to None (no explicit IR class
    attribute at all) — matches IR's own "absent = default class" rule,
    same asymmetry eagle_exporter applies on the way out (`class_num` there
    only ever maps NON-default class names to numbers ≥1).
    """
    class_name_by_num = {}
    classes_el = schematic_el.find('classes')
    if classes_el is None:
        return class_name_by_num
    classes_out = None
    for class_el in classes_el.findall('class'):
        number = class_el.get('number', '0')
        name = class_el.get('name', 'default')
        if number == '0' or name == 'default':
            class_name_by_num[number] = None
            continue
        class_name_by_num[number] = name
        if classes_out is None:
            classes_out = ET.SubElement(proj_el, 'classes')
        width_um = _um(class_el.get('width', '0'))
        drill_um = _um(class_el.get('drill', '0'))
        kwargs = {'name': name}
        if float(width_um):
            kwargs['width'] = width_um
        if float(drill_um):
            kwargs['drill'] = drill_um
        ET.SubElement(classes_out, 'class', **kwargs)
    return class_name_by_num


# ---------------------------------------------------------------------------
# Step 1/8: top-level entry point
# ---------------------------------------------------------------------------

def convert_project_full(src, output_path):
    """One Eagle .sch -> one .swprj <project>.

    A "project" here is just one schematic file — Eagle has no separate
    project-manifest/library-resolution-table precondition the way KiCad
    does (sym-lib-table/fp-lib-table): the .sch's own embedded
    <schematic><libraries> is self-sufficient (symbols+packages+devicesets),
    confirmed against every ground-truth fixture in this repo.
    """
    src = Path(src)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    root = ET.parse(src).getroot()
    schematic_el = root.find('.//schematic')
    if schematic_el is None:
        raise ValueError(f'{src}: no <schematic> element found.')

    proj_el = ET.Element('project', name=src.stem)
    symbols_el = ET.SubElement(proj_el, 'symbols')
    pool = {}

    comps_by_lib = _convert_embedded_libraries(schematic_el, symbols_el, proj_el, pool, src.stem)
    # (library, deviceset, technology) -> <component> — technology comes
    # from the <part>'s own technology= attribute (empty string for a part
    # that doesn't set one), matching convert_deviceset's own per-technology
    # component split.
    comp_by_name = {(lib_name, lib_urn, ds_name, tech_name): comp_el
                     for (lib_name, lib_urn), comps in comps_by_lib.items()
                     for (ds_name, tech_name), comp_el in comps.items()}

    class_name_by_num = _collect_classes(schematic_el, proj_el)
    ctx = {'class_name_by_num': class_name_by_num}

    # Nesting depth check (ir_schema.md "Вложенность запрещена", format-
    # agnostic invariant): a <module>'s own <sheet> must never carry a
    # non-empty <moduleinsts> — eagle.dtd's content model formally allows
    # it (sheet: description?, plain?, moduleinsts?, instances?, busses?,
    # nets?), so this is a defensive hard-fail, not dead code.
    modules_el = schematic_el.find('modules')
    module_defs = modules_el.findall('module') if modules_el is not None else []
    for mod_el in module_defs:
        for msheet_el in mod_el.findall('sheets/sheet'):
            minsts_el = msheet_el.find('moduleinsts')
            if minsts_el is not None and len(minsts_el):
                raise ValueError(
                    f'module "{mod_el.get("name")}": nested module instances found — '
                    f'module nesting is forbidden (depth must stay exactly 1).')

    # --- Modules: <module> definitions -> IR <module> (own canvas + ports).
    module_els = []   # this project's own <module> elements (dx/dy/port),
                       # needed below by _port_geometry to resolve <portref>
                       # wire stubs on the parent canvas — same element shape
                       # eagle_exporter._port_geometry already expects.
    for mod_el in module_defs:
        mod_name = mod_el.get('name')
        dx_um, dy_um = _um(mod_el.get('dx')), _um(mod_el.get('dy'))
        mod_out = ET.Element('module', name=mod_name, dx=dx_um, dy=dy_um)

        ports_el = mod_el.find('ports')
        if ports_el is not None:
            for port_el in ports_el.findall('port'):
                ET.SubElement(mod_out, 'port', name=port_el.get('name'),
                              direction=port_el.get('direction', 'io'),
                              side=port_el.get('side'), coord=_um(port_el.get('coord', '0')))

        mod_parts = _collect_parts(mod_el)
        mod_sheets = mod_el.findall('sheets/sheet')
        if not mod_sheets:
            raise ValueError(f'module "{mod_name}": no <sheet> found under <sheets>.')
        instances, nets = _collect_tiled_pages(
            mod_sheets, mod_parts, pool, comp_by_name, comps_by_lib, ctx,
            page_label=lambda idx: f'модуля "{mod_name}", страница {idx}')
        _write_canvas(mod_out, instances, nets)
        proj_el.append(mod_out)
        module_els.append(mod_out)

    # --- Top-level <parts>/<sheets>.
    top_parts = _collect_parts(schematic_el)
    sheets_el = schematic_el.find('sheets')
    sheet_els = sheets_el.findall('sheet') if sheets_el is not None else []
    if not sheet_els:
        raise ValueError(f'{src}: no <sheet> found under <sheets>.')

    schem_el = ET.SubElement(proj_el, 'schematic')

    # Module instances: <moduleinsts><moduleinst name= module= x= y= rot=?
    # offset=? modulevariant=?> -> IR <instance module=...>. modulevariant
    # (Eagle's variant-assembly system) is out of scope per ir_schema.md
    # "Варианты сборки" ("импорт из внешних форматов пока не реализован") —
    # logged if non-empty, not modeled. Collected via _collect_tiled_pages's
    # on_page hook — a module canvas never has <moduleinsts> of its own
    # (nesting forbidden), only top-level pages do.
    module_instances = []

    def _collect_moduleinsts(sheet_el, tile_x_offset):
        minsts_el = sheet_el.find('moduleinsts')
        if minsts_el is None:
            return None
        local_insts = []   # LOCAL (pre-tile-offset) — for _port_geometry only,
                            # matching the coordinate space _collect_sheet's
                            # wires are still in when this page is processed.
        for minst_el in minsts_el.findall('moduleinst'):
            variant = minst_el.get('modulevariant', '')
            if variant:
                import_log.log(minst_el.get('name'), variant,
                                'MODULE_VARIANT (modulevariant) not modeled, '
                                'assembly-variant import not yet implemented')
            angle_deg, mirror = parse_rot(minst_el.get('rot'))
            local_x_um = int(_um(minst_el.get('x')))
            y_um = int(_um(minst_el.get('y')))
            rot_str = str(round(angle_deg)) if angle_deg else '0'
            mirror_str = '1' if mirror else '0'
            local_insts.append({'module': minst_el.get('module'),
                                 'name': minst_el.get('name'),
                                 'x': str(local_x_um), 'y': str(y_um),
                                 'rot': rot_str, 'mirror': mirror_str})
            inst = {'module': minst_el.get('module'), 'designator': minst_el.get('name'),
                    'x': str(local_x_um + tile_x_offset), 'y': str(y_um),
                    'rot': rot_str, 'mirror': mirror_str}
            offset = minst_el.get('offset', '0')
            if offset != '0':
                inst['offset'] = offset
            module_instances.append(inst)
        return _port_geometry(module_els, local_insts)

    component_instances, all_nets = _collect_tiled_pages(
        sheet_els, top_parts, pool, comp_by_name, comps_by_lib, ctx,
        page_label=str, on_page=_collect_moduleinsts)

    # Module-instance <instance module=...> elements go on the canvas
    # alongside ordinary component instances (same parent_el, same content
    # model — ir_schema.md "Модуль").
    for inst in module_instances:
        kwargs = {'module': inst['module'], 'name': inst['designator'],
                  'x': inst['x'], 'y': inst['y'], 'rot': inst['rot'], 'mirror': inst['mirror']}
        if inst.get('offset'):
            kwargs['offset'] = inst['offset']
        ET.SubElement(schem_el, 'instance', **kwargs)

    _write_canvas(schem_el, component_instances, all_nets)

    # --- Board half: a sibling .brd makes this project ONE <layout>
    # (Eagle can't have more than one board per schematic). Import is
    # project-scoped by design — element->instance binding is REFDES
    # identity, which only the schematic can vouch for.
    brd_path = src.with_suffix('.brd')
    if brd_path.exists():
        from babel.eagle_board_parser import convert_board

        # Eagle flattens module-instance parts onto the board two ways
        # (modtest ground truth): 'NAMUR3:C1' natively (offset-less
        # instance) — already the IR canon INST:REFDES — and numerically
        # ('C101' = C1 + moduleinst offset=100). Map the numeric spelling
        # back; pure recorded-offset arithmetic, no guessing.
        name_map = {}
        known = {i['designator'] for i in component_instances}
        mod_parts = {m.get('name'): [mi.get('name') for mi in m.findall('instance')
                                     if not mi.get('module')]
                     for m in module_els}
        for minst in module_instances:
            for part in mod_parts.get(minst['module'], ()):
                addr = f'{minst["designator"]}:{part}'
                known.add(addr)
                offset = minst.get('offset')
                if offset and offset != '0':
                    m = re.match(r'^(.*?)(\d+)$', part)
                    if m:
                        flat = f'{m.group(1)}{int(m.group(2)) + int(offset)}'
                        name_map[flat] = addr

        net_names = {n.get('name') for n in schem_el.findall('net')}
        board_attrs = {}
        layout_el = convert_board(brd_path, name_map=name_map, known=known,
                                  net_names=net_names, attr_sink=board_attrs)

        # Board-side attribute values merge into the SHARED instance —
        # Eagle allows editing/adding element attributes right in the board
        # editor without syncing the .sch part (luminoso ground truth: 75
        # elements with brd-only attrs), while the IR attribute model keeps
        # ONE value home. Union rule, deterministic: absent -> add (logged);
        # empty vs non-empty -> non-empty wins; two DIFFERENT non-empty
        # values -> hard reject (no side is authoritative, a human must
        # pick). Module-instance parts merge across ALL siblings with the
        # same rules (per-sibling divergence of non-empty values is
        # inexpressible: the module canvas is shared).
        # FIRST-wins on duplicate names: a multi-gate part is several
        # <instance> elements sharing one designator, and the importer puts
        # the part's attrs on the FIRST gate — the merge must target the
        # same element (last-wins silently stranded merged attrs on a gate
        # nobody reads: PowerPCB module LMV324, 4 gates).
        inst_el_by_name = {}
        for i in schem_el.findall('instance'):
            inst_el_by_name.setdefault(i.get('name'), i)
        mod_inst_by_addr = {}
        for m in module_els:
            for mi_el in m.findall('instance'):
                if mi_el.get('module'):
                    continue
                for minst in module_instances:
                    if minst['module'] == m.get('name'):
                        addr = f'{minst["designator"]}:{mi_el.get("name")}'
                        mod_inst_by_addr.setdefault(addr, mi_el)
        for el_name, battrs in board_attrs.items():
            # explicit None test: a childless ET.Element is FALSY, `or`
            # would drop every instance that has no <attr> children yet —
            # exactly the ones this merge exists to fill
            target = inst_el_by_name.get(el_name)
            if target is None:
                target = mod_inst_by_addr.get(el_name)

            if target is None:
                continue
            existing = {a.get('name'): a for a in target.findall('attr')}
            added = []
            for aname, bval in battrs.items():
                cur = existing.get(aname)
                if cur is None:
                    ET.SubElement(target, 'attr', name=aname, value=bval)
                    existing[aname] = target[-1]
                    added.append(aname)
                    continue
                cval = cur.get('value') or ''
                if cval == bval or not bval:
                    continue
                if not cval:
                    cur.set('value', bval)
                    added.append(aname)
                    continue
                raise ValueError(
                    f'{brd_path}: element {el_name!r} attribute {aname!r} '
                    f'diverges between board ({bval!r}) and schematic '
                    f'({cval!r}) — no side is authoritative, reconcile in '
                    f'Eagle and re-import')
            if added:
                import_log.log(el_name, ','.join(sorted(added)),
                               'BOARD_ATTRS merged into shared instance')
        # every board element must resolve — diagnostics over silence
        unresolved = sorted({e.get('name') for e in layout_el
                             if e.tag == 'element'
                             and not e.get('footprint')} - known)
        if unresolved:
            raise ValueError(
                f'{brd_path}: board elements with no schematic instance: '
                f'{", ".join(unresolved[:10])} — REFDES identity broken')
        proj_el.append(layout_el)

    tree_str = ET.tostring(proj_el, encoding='unicode')
    from xml.dom import minidom
    xml_str = minidom.parseString(tree_str).toprettyxml(indent='  ')
    lines = [l for l in xml_str.splitlines() if l.strip()]
    result = '<?xml version="1.0" encoding="utf-8"?>\n' + '\n'.join(lines[1:])
    output_path.write_text(result, encoding='utf-8')
    print(f'Written: {output_path}')
    import_log.write(output_path)
    return result


if __name__ == '__main__':
    import sys
    sch = sys.argv[1] if len(sys.argv) > 1 else 'testData/min.sch'
    out = sys.argv[2] if len(sys.argv) > 2 else 'outputs/min.swprj'
    convert_project_full(sch, out)
