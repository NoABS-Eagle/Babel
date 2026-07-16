"""IR <layout> -> Eagle .brd (ir_schema.md "Плата (Board IR)").

The inverse of eagle_board_parser.convert_board, sharing the geometry
emitters and layer math with eagle_exporter (packages/symbols/schematic).
Element->package binding comes from the SHARED schematic instance (REFDES
identity): a <layout> can only be exported as part of a project that has
its <schematic>/<component> sections — exactly how the import side works.
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from babel import import_log
from babel.eagle_exporter import (_LAYERS_FILE, _eagle_designator, _eagle_name,
                                  _emit_arc, _emit_geometry, _geom_sig,
                                  _pkg_eagle_layer, _resolved_attrs, _tomm,
                                  export_package)
from babel.ir_util import parse_layer, parse_stack

_GEOM_TAGS = ('line', 'arc', 'shape', 'polygon', 'text', 'hole')


def _stack(layout_el):
    """Ordered Eagle copper numbers for this layout, top first. The copper
    COUNT comes from the stack formula (ir_schema.md "Формула стека" — the
    one authoritative place); the Eagle NUMBERING prefers the original
    layerSetup from the eagle <passthrough> (keeps Eagle's both-ends inner
    numbering stable on round-trip), falling back to the canonical
    1, 2..N-1, 16."""
    copper = len(parse_stack(layout_el.get('stack'))[0])
    pt = layout_el.find("passthrough[@tool='eagle']")
    if pt is not None:
        dr = pt.find('designrules')
        if dr is not None:
            for p in dr.findall('param'):
                if p.get('name') == 'layerSetup':
                    nums = [int(t) for t in re.findall(r'\d+', p.get('value', ''))]
                    if len(nums) == copper:
                        return nums
    return [1] + list(range(2, copper)) + [16]


def _apply_stack_to_designrules(dr, eagle_stack, layout_el):
    """Write the IR stack formula's thicknesses back into the passthrough
    designrules cells (mtCopper/mtIsolate, indexed by Eagle layer NUMBER —
    ground truth maximus.brd). The formula is the authoritative stack fact;
    the passthrough copy is tool state that must not contradict it after an
    IR-side edit. Cells of unused layers keep their (junk) values. ε/tanδ
    have no Eagle home — they simply don't travel (Eagle never knew them)."""
    coppers, dielectrics = parse_stack(layout_el.get('stack'))
    by_name = {p.get('name'): p for p in dr.findall('param')}

    def patch(name, pairs):
        p = by_name.get(name)
        if p is None:
            return
        cells = p.get('value', '').split()
        for eagle_n, um in pairs:
            if eagle_n - 1 < len(cells):
                cells[eagle_n - 1] = f'{um / 1000:g}mm'
        p.set('value', ' '.join(cells))

    patch('mtCopper', zip(eagle_stack, coppers))
    patch('mtIsolate',
          zip(eagle_stack[:-1], (d[0] for d in dielectrics)))


def _copper_num(ir_layer, stack):
    """IR copper layer attr ('1'/'-1'/'2'..; optional '!') -> (anti, eagle_n).
    Inverse of eagle_board_parser._copper_map: inner IR k = stack[k-1]."""
    anti, n = parse_layer(ir_layer)
    if n == 1:
        return anti, stack[0]
    if n == -1:
        return anti, stack[-1]
    return anti, stack[n - 1]


def _fold_plating(els):
    """The cut/PLATING twin fold (ir_schema.md "Резы и металлизация"): ids
    of 120-cuts to emit on 46 Milling, and ids of their consumed 146 twins."""
    pool = {}
    for el in els:
        if el.get('layer') == '146':
            pool.setdefault(_geom_sig(el), []).append(id(el))
    milled, folded = set(), set()
    for el in els:
        if el.get('layer') == '120':
            q = pool.get(_geom_sig(el))
            if q:
                folded.add(q.pop())
                milled.add(id(el))
    return milled, folded


def _emit_signal_polygon(sig_out, el, eagle_n, anti):
    pg = ET.SubElement(sig_out, 'polygon')
    pg.set('width', _tomm(el.get('width', '0')))
    pg.set('layer', str(eagle_n))
    if anti:
        pg.set('pour', 'cutout')
    fill = float(el.get('fill', '100'))
    if 0 < fill < 100:
        # inverse of the import formula fill = width/spacing*100
        pg.set('pour', 'hatch')
        pg.set('spacing', _tomm(str(round(float(el.get('width', '0')) * 100 / fill))))
    if el.get('clearance'):
        pg.set('isolate', _tomm(el.get('clearance')))
    if el.get('rank'):
        pg.set('rank', el.get('rank'))
    if el.get('thermals') == '0':
        pg.set('thermals', 'no')
    for v in el.findall('vertex'):
        ve = ET.SubElement(pg, 'vertex')
        ve.set('x', _tomm(v.get('x'))); ve.set('y', _tomm(v.get('y')))
        if v.get('curve'):
            ve.set('curve', v.get('curve'))


def _element_rot(e):
    rot = float(e.get('rot', 0))
    bottom = e.get('side') == 'bottom'
    if bottom:
        # inverse of the parser's Eagle MR{α} -> IR −α (rotate-then-mirror
        # vs IR's mirror-then-rotate) — MR0 must stay explicit, M carries
        # the side
        return f'MR{(-rot) % 360:g}'
    return f'R{rot:g}' if rot else None


_EAGLE_SILK = {21, 22, 25, 26, 27, 28}   # tPlace/bPlace, tNames/…, tValues/…


def _emit_element_attribute(el_out, t, e, force_layer=None):
    """IR <text> placeholder (footprint default OR element override) ->
    Eagle <attribute> (absolute board coords/angle — the exact inverse of
    _convert_element's localization). `force_layer` overrides the mapped
    layer (NAME/VALUE go on their canonical tNames/tValues, not the
    footprint placeholder's own silk layer)."""
    import math
    a = ET.SubElement(el_out, 'attribute')
    a.set('name', (t.text or '').strip().lstrip('>'))
    ex, ey = float(e.get('x')), float(e.get('y'))
    rot = float(e.get('rot', 0))
    mirror = e.get('side') == 'bottom'
    if t.get('hidden') == 'yes':
        a.set('x', _tomm(e.get('x'))); a.set('y', _tomm(e.get('y')))
        a.set('size', '1.778'); a.set('layer', '27')
        a.set('display', 'off')
        return
    lx, ly = float(t.get('x', 0)), float(t.get('y', 0))
    lrot = float(t.get('rot', 0))
    if mirror:
        lx = -lx
        # lrot NOT pre-negated: the mirror branch of the arot formula is
        # the whole inverse (same double-flip as the schematic side)
    r = math.radians(rot)
    ax = ex + lx * math.cos(r) - ly * math.sin(r)
    ay = ey + lx * math.sin(r) + ly * math.cos(r)
    arot = (rot + lrot) % 360 if not mirror else (rot - lrot) % 360
    a.set('x', _tomm(str(round(ax)))); a.set('y', _tomm(str(round(ay))))
    amirror = (t.get('mirror') == '1') != mirror
    a.set('size', _tomm(t.get('size', '1778')))
    if force_layer is not None:
        a.set('layer', str(force_layer))
    else:
        eagle_layer = _pkg_eagle_layer(t.get('layer') or '125')
        a.set('layer', str(eagle_layer if eagle_layer is not None else 25))
    if amirror:
        # arot is the IR-absolute placed angle; Eagle MR wants its own
        # rotate-then-mirror reading — the −α inverse, as everywhere
        a.set('rot', f'MR{(-arot) % 360:g}')
    elif arot:
        a.set('rot', f'R{arot:g}')
    if t.get('font') == 'vector':
        a.set('font', 'vector')
    if t.get('ratio'):
        a.set('ratio', t.get('ratio'))
    align = t.get('align', 'bottom-left')
    if align != 'bottom-left':
        a.set('align', align)


def _instance_footprint(comp_el, inst_el):
    """The footprint this instance actually uses (ir_schema.md "Component
    instance": `footprint` attr recorded only for 2+-footprint components)."""
    fps = comp_el.findall('footprint')
    want = inst_el.get('footprint')
    if want:
        for fp in fps:
            if want in (fp.get('variant'), fp.get('name')):
                return fp
    return fps[0] if fps else None


def export_board(ir_path, output_path=None, layout_name=None):
    """IR project -> one Eagle .brd for the named (or single) <layout>."""
    root = ET.parse(ir_path).getroot()
    layouts = root.findall('layout')
    if not layouts:
        raise ValueError(f'{ir_path}: no <layout> — nothing to export as a board')
    layout = next((l for l in layouts
                   if layout_name in (None, l.get('name'))), None)
    if layout is None:
        raise ValueError(f'{ir_path}: no layout named {layout_name!r}')

    schem = root.find('schematic')
    comp_by_name = {c.get('name'): c for c in root.findall('component')}
    # first-wins: multi-gate parts share a designator across instances and
    # the attribute home is the FIRST gate (importer convention)
    inst_by_des = {}
    if schem is not None:
        for i in schem.findall('instance'):
            inst_by_des.setdefault(i.get('name'), i)
    module_by_name = {m.get('name'): m for m in root.findall('module')}
    local_fp = {f.get('name'): f for f in layout.findall('footprint')}

    def _resolve_instance(des):
        """IR element address -> (instance, eagle board designator).
        'INST:REFDES' (module-instance part, ir_schema.md "<element>") uses
        the module canvas instance; the Eagle spelling flattens numerically
        when the moduleinst carries offset= (C1 @ offset 100 -> C101), and
        keeps the native colon form otherwise — exact import inverse."""
        if ':' not in des:
            return inst_by_des.get(des), des
        minst_name, part = des.split(':', 1)
        minst = inst_by_des.get(minst_name)
        mod = module_by_name.get(minst.get('module')) if minst is not None else None
        if mod is None:
            return None, des
        part_inst = next((i for i in mod.findall('instance')
                          if i.get('name') == part), None)
        offset = minst.get('offset')
        if offset and offset != '0':
            m = re.match(r'^(.*?)(\d+)$', part)
            if m:
                return part_inst, f'{m.group(1)}{int(m.group(2)) + int(offset)}'
        return part_inst, des

    stack = _stack(layout)

    eagle = ET.Element('eagle')
    eagle.set('version', '9.6.2')   # the dialect our ground truths came from; a 7.7.0 tag made Eagle 9 take its legacy text path and Cyrillic vector text stopped rendering (user ground truth)
    drawing = ET.SubElement(eagle, 'drawing')
    settings = ET.SubElement(drawing, 'settings')
    ET.SubElement(settings, 'setting').set('alwaysvectorfont', 'no')
    ET.SubElement(settings, 'setting').set('verticaltext', 'up')
    # grid: the source's own settings via the eagle passthrough (tool
    # state, verbatim — same as the layer table below); defaults only for
    # IR-born projects
    _pt_grid = layout.find("passthrough[@tool='eagle']/grid")
    if _pt_grid is not None:
        import copy as _copy
        drawing.append(_copy.deepcopy(_pt_grid))
    else:
        grid = ET.SubElement(drawing, 'grid')
        for k, v in [('distance', '0.1'), ('unitdist', 'inch'), ('unit', 'inch'),
                     ('style', 'lines'), ('multiple', '1'), ('display', 'no'),
                     ('altdistance', '0.01'), ('altunitdist', 'inch'), ('altunit', 'inch')]:
            grid.set(k, v)
    # layer table: the SOURCE's own <layers> (visibility selection, user
    # layer names/colors) travels via the eagle passthrough and comes back
    # verbatim; only an IR-born project (no passthrough) gets the synthetic
    # table, with copper activation following the actual stack (the static
    # table once marked all 16 copper active — phantom inner layers).
    pt_pre = layout.find("passthrough[@tool='eagle']")
    src_layers = pt_pre.find('layers') if pt_pre is not None else None
    if src_layers is not None:
        import copy as _copy
        drawing.append(_copy.deepcopy(src_layers))
    else:
        layers_root = ET.parse(_LAYERS_FILE).getroot()
        for l in layers_root:
            n = int(l.get('number'))
            if 1 <= n <= 16:
                in_stack = n in stack
                l.set('active', 'yes' if in_stack else 'no')
                l.set('visible', 'yes' if in_stack else 'no')
        drawing.append(layers_root)

    board = ET.SubElement(drawing, 'board')
    plain = ET.SubElement(board, 'plain')

    # --- free geometry, with the cut/PLATING fold
    free = [el for el in layout if el.tag in _GEOM_TAGS]
    milled, folded = _fold_plating(free)
    for el in free:
        if id(el) in folded:
            continue
        if el.tag == 'hole':
            _emit_geometry(plain, el, 0)     # layer-less by nature
            continue
        num = 46 if id(el) in milled else _pkg_eagle_layer(el.get('layer'))
        if num is None:
            import_log.log(layout.get('name'), el.tag,
                           'BOARD_EXPORT dropped, no Eagle layer for',
                           str(el.get('layer')))
            continue
        _emit_geometry(plain, el, num)

    # --- elements + the libraries (packages only) they need
    elements_ir = layout.findall('element')
    el_binding = {}          # IR address -> (lib, comp, inst, fp, eagle_name)
    for e in elements_ir:
        des = e.get('name')
        if e.get('footprint'):
            # board-only padless object: footprint embedded per-layout
            fp = local_fp.get(e.get('footprint'))
            if fp is None:
                raise ValueError(f'{ir_path}: element {des!r} references '
                                 f'layout footprint {e.get("footprint")!r} '
                                 f'that is not embedded')
            el_binding[des] = (fp.get('library') or 'babel', None, None, fp, des)
            continue
        inst, eagle_name = _resolve_instance(des)
        comp = comp_by_name.get(inst.get('component')) if inst is not None else None
        fp = _instance_footprint(comp, inst) if comp is not None else None
        if fp is None:
            raise ValueError(
                f'{ir_path}: element {des!r} has no schematic instance with a '
                f'footprint — REFDES identity broken, cannot export the board')
        el_binding[des] = (comp.get('library') or root.get('name') or 'babel',
                          comp, inst, fp, eagle_name)

    libraries_el = ET.SubElement(board, 'libraries')
    pkgs_by_lib = {}
    for lib, comp, inst, fp, eagle_name in el_binding.values():
        pkgs_by_lib.setdefault(_eagle_name(lib), {})[fp.get('name')] = fp
    for lib_name, fps in sorted(pkgs_by_lib.items()):
        lib_el = ET.SubElement(libraries_el, 'library', name=lib_name)
        packages_el = ET.SubElement(lib_el, 'packages')
        for pkg_name, fp in sorted(fps.items()):
            packages_el.append(export_package(fp, pkg_name))

    # layout <attr> children = the board's global attributes (tool bags like
    # the user's NOABS_* 3D-generator settings travel verbatim, uninterpreted)
    attributes_el = ET.SubElement(board, 'attributes')
    for a in layout.findall('attr'):
        ET.SubElement(attributes_el, 'attribute', name=a.get('name'),
                      value=a.get('value', ''))
    ET.SubElement(board, 'variantdefs')

    # --- classes: same name->number enumeration as export_schematic, so a
    # signal's class number means the same thing in both files of the pair
    classes_el = ET.SubElement(board, 'classes')
    ET.SubElement(classes_el, 'class', number='0', name='default',
                  width='0', drill='0')
    class_num = {}
    ir_classes = root.find('classes')
    if ir_classes is not None:
        for i, cl in enumerate(ir_classes.findall('class'), start=1):
            num = str(i)
            cl_el = ET.SubElement(classes_el, 'class', number=num,
                                  name=_eagle_name(cl.get('name')),
                                  width=_tomm(cl.get('width', '0')),
                                  drill=_tomm(cl.get('drill', '0')))
            if cl.get('clearance'):
                ET.SubElement(cl_el, 'clearance', **{
                    'class': num, 'value': _tomm(cl.get('clearance'))})
            class_num[cl.get('name')] = num

    # --- passthrough: designrules/autorouter now, errors after signals
    pt = layout.find("passthrough[@tool='eagle']")
    errors_el = None
    if pt is not None:
        for tag in ('designrules', 'autorouter'):
            src_el = pt.find(tag)
            if src_el is not None:
                if tag == 'designrules':
                    _apply_stack_to_designrules(src_el, stack, layout)
                board.append(src_el)
        errors_el = pt.find('errors')

    elements_el = ET.SubElement(board, 'elements')
    for e in elements_ir:
        des = e.get('name')
        lib, comp, inst, fp, eagle_name = el_binding[des]
        el_out = ET.SubElement(elements_el, 'element')
        el_out.set('name', _eagle_designator(eagle_name))
        el_out.set('library', _eagle_name(lib))
        el_out.set('package', _eagle_name(fp.get('name')))
        value = ''
        if inst is not None and comp is not None:
            # IR's VALUE concept = the (lowercase) 'value' attribute on the
            # shared instance — the schematic importer's naming convention.
            # uservalue != yes and no explicit value -> Eagle's own
            # derivation, which it BAKES into element@value on the board
            # (unlike the .sch part, where an absent value= suffices):
            # deviceset+technology+device. The IR component name IS
            # deviceset+technology (per-technology component split) and the
            # footprint variant keeps the device name verbatim incl. its
            # leading dash ('-SOT89'). With uservalue="yes" the value is
            # the user's text and EMPTY is a legal state (luminoso SJ1).
            value = _resolved_attrs(comp, inst).get('value', '')
            if not value and comp.get('uservalue') != 'yes':
                value = (_eagle_name(comp.get('renamed-from') or comp.get('name'))
                         + (fp.get('variant') or ''))
        el_out.set('value', value)
        el_out.set('x', _tomm(e.get('x'))); el_out.set('y', _tomm(e.get('y')))
        rot = _element_rot(e)
        # Visible silk text on a board element = the FOOTPRINT's >NAME/
        # >VALUE placeholders (источник истины — библиотека), NOT whatever
        # the element instance happens to carry: an Eagle element shows text
        # only via <attribute> records, and Eagle's own "Restore Position"
        # materializes one per package placeholder. The KiCad round-trip
        # gives the element ZERO overrides (nothing moved) — driving the set
        # from the footprint is what keeps >NAME from vanishing (RC board:
        # R1 came back with no refdes text). A placeholder on a non-silk
        # layer (a KiCad Value fp_text on F.Fab) is NOT shown.
        overrides = {(t.text or '').strip().lstrip('>').upper(): t
                     for t in e.findall('text')}
        fp_texts = {(t.text or '').strip().lstrip('>').upper(): t
                    for t in fp.findall('text')
                    if (t.text or '').strip().startswith('>')}
        if fp_texts:
            el_out.set('smashed', 'yes')
        if rot:
            el_out.set('rot', rot)
        emitted = set()
        for nm, fpt in fp_texts.items():
            ov = overrides.get(nm)
            src = ov if (ov is not None and (ov.get('x') is not None
                         or ov.get('hidden') == 'yes')) else fpt
            eagle_layer = _pkg_eagle_layer(fpt.get('layer') or '121')
            if nm == 'NAME':
                _emit_element_attribute(el_out, src, e, force_layer=25)
            elif eagle_layer in _EAGLE_SILK:
                _emit_element_attribute(el_out, src, e,
                                        force_layer=27 if nm == 'VALUE' else None)
            else:
                continue           # non-silk placeholder (fab value) — not shown
            emitted.add(nm)
        if inst is not None and comp is not None:
            # Eagle keeps a value copy of every part attribute ON the board
            # element (editable from the board editor; can even exist only
            # there) — regenerate the copies from the shared instance, the
            # IR's one value home. Hidden records: element coords, tValues.
            # Names go back UPPERCASE — Eagle's own canon (its UI uppercases
            # attribute names on entry), inverse of the import lowercasing.
            emitted_l = {n.lower() for n in emitted}
            for aname, aval in sorted(_resolved_attrs(comp, inst).items()):
                # 'value'/'description' in the component attribute container
                # are IR bookkeeping (deviceset description text, value
                # default) — not Eagle attributes, never baked onto elements
                if aname.lower() in emitted_l or \
                        aname.lower() in ('name', 'value', 'description'):
                    continue
                a = ET.SubElement(el_out, 'attribute')
                a.set('name', aname.upper()); a.set('value', aval)
                a.set('x', _tomm(e.get('x'))); a.set('y', _tomm(e.get('y')))
                a.set('size', '1.778'); a.set('layer', '27')
                if rot:
                    a.set('rot', rot.lstrip('M'))
                a.set('display', 'off')

    signals_el = ET.SubElement(board, 'signals')
    net_class = ({n.get('name'): n.get('class') for n in schem.findall('net')}
                 if schem is not None else {})
    for sig in layout.findall('signal'):
        s_out = ET.SubElement(signals_el, 'signal')
        s_out.set('name', _eagle_designator(sig.get('name')))
        cls = class_num.get(net_class.get(sig.get('name')), '0')
        if cls != '0':
            s_out.set('class', cls)
        for c in sig:
            if c.tag == 'contactref':
                cr = ET.SubElement(s_out, 'contactref')
                el_ref = c.get('element')
                eagle_ref = (el_binding[el_ref][4] if el_ref in el_binding
                             else _resolve_instance(el_ref)[1])
                cr.set('element', _eagle_designator(eagle_ref))
                cr.set('pad', _eagle_designator(c.get('pad')))
            elif c.tag == 'line':
                anti, n = _copper_num(c.get('layer'), stack)
                w = ET.SubElement(s_out, 'wire')
                w.set('x1', _tomm(c.get('x1'))); w.set('y1', _tomm(c.get('y1')))
                w.set('x2', _tomm(c.get('x2'))); w.set('y2', _tomm(c.get('y2')))
                w.set('width', _tomm(c.get('width', '0')))
                w.set('layer', str(n))
            elif c.tag == 'arc':
                _, n = _copper_num(c.get('layer'), stack)
                _emit_arc(s_out, c, str(n))
            elif c.tag == 'via':
                v = ET.SubElement(s_out, 'via')
                v.set('x', _tomm(c.get('x'))); v.set('y', _tomm(c.get('y')))
                v.set('extent', f'{stack[0]}-{stack[-1]}')
                v.set('drill', _tomm(c.get('drill')))
                if c.get('diameter'):
                    v.set('diameter', _tomm(c.get('diameter')))
                if c.get('shape') and c.get('shape') != 'round':
                    v.set('shape', c.get('shape'))
            elif c.tag == 'polygon':
                anti, n = _copper_num(c.get('layer'), stack)
                _emit_signal_polygon(s_out, c, n, anti)

    if errors_el is not None:
        board.append(errors_el)

    xml_str = minidom.parseString(ET.tostring(eagle, encoding='unicode')) \
                     .toprettyxml(indent='  ')
    clean = '\n'.join(l for l in xml_str.splitlines() if l.strip())
    result = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<!DOCTYPE eagle SYSTEM "eagle.dtd">\n' +
              '\n'.join(clean.splitlines()[1:]))
    if output_path:
        Path(output_path).write_text(result, encoding='utf-8')
        print(f'Written: {output_path}  ({len(elements_ir)} element(s), '
              f'{len(layout.findall("signal"))} signal(s), '
              f'{len(stack)} copper layer(s))')
    return result


if __name__ == '__main__':
    import sys
    export_board(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
