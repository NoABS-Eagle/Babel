"""IR .swprj -> full self-contained KiCad project exporter.

Closes the round-trip linter loop (decisions.md "Round-trip как линтер"):
kicad_project_parser.convert_project_full on this module's own output must
yield an IR equivalent to the input. Plan of record:
C:/Users/j3qq4hch/.claude/plans/dynamic-bouncing-cerf.md.

Format notes:
  - S-expressions are assembled as strings, same as kicad_exporter.py —
    kiutils is READ-only in this project (deprecated dependency, its writer
    emits a KiCad-6-era dialect; see progress.md "kiutils — deprecated").
  - File version token is 20260306 — ground truth from real KiCad 10.0
    output (user-saved channel_strip.kicad_sch). Supply symbols inside
    MODULE sheet files are exported with `(power local)` (KiCad 10 scopes
    the net to the file = IR module semantics), top-level pages with
    `(power global)` — see _lib_symbols_cache. kiutils silently drops the
    local/global argument, so the import side reads it via a raw-sexpr
    side-channel (kicad_project_parser._power_locality) — the first
    confirmed kiutils silent-loss case, see decisions.md.
  - GUARANTEE: no global_label is ever emitted, in any file, under any
    condition (decisions.md; the round-trip test greps for it). Net names
    travel via local labels + hierarchical labels on module ports only.

Coordinate model: IR canvas is Y-up µm with pages tiled horizontally, each
page marked by a FRAME-bearing instance (ir_schema.md "Frame"). Every page
becomes its own .kicad_sch with the frame's bbox mapped to the paper:
kicad_x = ir_x - frame_left, kicad_y = frame_top - ir_y (single Y flip).
Instance rot/mirror are a LITERAL copy back (ir_rot -> angle, ir_mirror=1 ->
`(mirror y)`) — the exact inverse image of kicad_schematic._mirror_and_angle,
no compensation formulas (decisions.md "KiCad: ir_rot/ir_mirror —
литеральное копирование, не формула", the three-revisions lesson: only the
user looking at real KiCad can judge this).
"""
import json
import math
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from babel import import_log
from babel.ir_util import (symbol_pool, component_gates, sanitize_filename,
                            rotate_port_side, arc_mid, instance_designator)
from babel.kicad_schematic import _collinear_between
from babel.kicad_exporter import (
    export as export_library, export_symbol,
    _f, _mm, _q, _justify, _build_pin_map, _eagle_overbar_to_kicad,
    _norm_text_angle,
)

_SCH_VERSION = 20260306   # ground truth: real KiCad 10.0 output (user-saved
                          # channel_strip.kicad_sch with a local supply)
_GENERATOR = 'babel'

# ISO paper sizes, PORTRAIT (narrow-first) mm — same table as
# kicad_project_parser._ISO_PAPER_MM (kept separate: importer/exporter don't
# share state, and the reverse lookup below is exporter-only).
_ISO_PAPER_MM = {
    'A5': (148, 210), 'A4': (210, 297), 'A3': (297, 420),
    'A2': (420, 594), 'A1': (594, 841), 'A0': (841, 1189),
}

# IR <port> direction -> KiCad hierarchical_label / sheet pin shape token.
# Inverse of kicad_project_parser._PORT_DIRECTION; `pwr` has no hier-label
# shape of its own in KiCad -> passive (logged at emission).
_PORT_SHAPE = {'in': 'input', 'out': 'output', 'io': 'bidirectional',
               'pas': 'passive', 'pwr': 'passive'}

# Sheet-pin / hierarchical-label text angle per block side. All four now
# ground-truthed (testData/multichannel_rotated — the user rotated/mirrored
# sheet instances of OUR OWN export in real KiCad 10, which re-laid the
# pins out on every edge): right=0, left=180, top=90, bottom=270. The
# previous top/bottom values (270/90, guessed as "rotational continuation")
# were exactly inverted.
_SIDE_ANGLE = {'right': 0, 'left': 180, 'top': 90, 'bottom': 270}


def _quuid(ns, *key):
    """Deterministic uuid5 from a stable string key — reproducible diffs
    between runs (plan: "UUID детерминированные")."""
    return str(uuid.uuid5(ns, '/'.join(str(k) for k in key)))


def _qt(s):
    """Quote text that may carry real newlines: KiCad stores a line break in
    graphic text as the literal 2-char `\\n` escape (see
    kicad_parser._kicad_multiline_to_eagle — IR canon is a real newline,
    converted at the KiCad boundary in both directions)."""
    s = str(s).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f'"{s}"'


def _paper_from_frame(width_mm, height_mm):
    """Frame size -> (paper_clause, warning_or_None). Landscape ISO matches
    get the bare name (KiCad's schematic default orientation, see
    kicad_project_parser._paper_dims_mm); portrait adds the token; anything
    else -> explicit "User" size."""
    for name, (w, h) in _ISO_PAPER_MM.items():
        if (round(width_mm), round(height_mm)) == (h, w):
            return f'(paper "{name}")', None
        if (round(width_mm), round(height_mm)) == (w, h):
            return f'(paper "{name}" portrait)', None
    return (f'(paper "User" {_f(width_mm)} {_f(height_mm)})',
            f'non-ISO frame {width_mm:g}x{height_mm:g}mm -> paper "User"')


# ---------------------------------------------------------------------------
# IR access helpers
# ---------------------------------------------------------------------------

def _is_frame_component(comp_el, pool):
    """Same rule as eagle_exporter._is_frame_component (a FRAME-layer shape
    on the symbol) — duplicated rather than imported: eagle_exporter is a
    sibling consumer, not a library for this module."""
    for _, sym_name in component_gates(comp_el):
        sym_el = pool.get(sym_name)
        if sym_el is not None and any(s.get('layer') == 'FRAME'
                                       for s in sym_el.findall('shape')):
            return True
    return False


def _frame_bbox(inst_el, comp_el, pool):
    """Absolute µm (x1, x2, y1, y2) of a frame instance's FRAME shape.
    Frame instances are synthesized rot=0/mirror=0 on every import path
    (ir_schema.md "Frame") — pure translation."""
    sym_name = component_gates(comp_el)[0][1]
    shape = next(s for s in pool[sym_name].findall('shape')
                 if s.get('layer') == 'FRAME')
    ix, iy = float(inst_el.get('x')), float(inst_el.get('y'))
    cx, cy = ix + float(shape.get('x')), iy + float(shape.get('y'))
    w, h = float(shape.get('w')), float(shape.get('h'))
    return cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2


def _resolved_attrs(comp_el, inst_el):
    """Component attrs overlaid with instance overrides (instance wins) —
    same precedence as everywhere else in this project."""
    attrs = {}
    attrs_el = comp_el.find('attributes')
    if attrs_el is not None:
        attrs = {a.get('name'): a.get('value', '') for a in attrs_el.findall('attr')}
    attrs.update({a.get('name'): a.get('value', '') for a in inst_el.findall('attr')})
    return attrs


def _sup_pin_names(comp_el, pool):
    """Bare names of `sup`-direction pins across the component's gates —
    a net named by one of these is self-named in KiCad too (the supply
    symbol's Value names the net), so no label synthesis is needed."""
    names = set()
    for _, sym_name in component_gates(comp_el):
        sym_el = pool.get(sym_name)
        if sym_el is None:
            continue
        for p in sym_el.findall('pin'):
            if p.get('direction') == 'sup':
                names.add(p.get('name'))
    return names


def _placeholder_styles(sym_el):
    """{'NAME'|'VALUE'|attr_lower: (x_um, y_um, rot, size_um, align)} from the
    symbol's >XXX placeholder texts — drives placed-property positioning,
    mirroring what export_symbol bakes into the library symbol."""
    styles = {}
    for el in sym_el.findall('text'):
        txt = (el.text or '').strip()
        if not txt.startswith('>'):
            continue
        key = txt[1:]
        key = key if key in ('NAME', 'VALUE') else key.lower()
        styles[key] = (float(el.get('x', 0)), float(el.get('y', 0)),
                       float(el.get('rot', 0)) % 360,
                       float(el.get('size', '1270')),
                       el.get('align', 'bottom-left'))
    return styles


def _inst_point(lx_um, ly_um, inst_el):
    """Symbol-local point (µm, Y-up) -> absolute IR canvas point (µm, Y-up)
    for FIELD/placeholder placement under the instance transform.

    Mirror negates local X (mirror-Y convention, matching the `(mirror y)` we
    emit) AND reverses the rotation sense. That theta flip is what makes this
    DIVERGE, deliberately, from the body/pin transform (svg_renderer._inst_point
    / _abs_pin_pos_mm use plain mirror-then-rotate-CCW): KiCad applies a
    DIFFERENT rule to a mirrored symbol's field anchors than to its body —
    their own field-mirror vs body-mirror inconsistency. Without the flip the
    fields of a mirrored, rotated symbol land 180° off (Bug 1). Ground-truthed
    against six hand-placed IRLML9301 instances in real KiCad 10 (rot 0/90/180/
    270 × mirror none/x/y): the negate-theta form reproduces all six field
    offsets exactly; the old CCW form matched only the unmirrored + mirror@rot0
    cases and inverted mirror@rot90/270. Pin connectivity is untouched (KiCad
    recomputes pins itself from `(mirror y)`+angle — this function never moves
    them)."""
    x, y = lx_um, ly_um
    theta = math.radians(float(inst_el.get('rot', '0')))
    if inst_el.get('mirror') == '1':
        x = -x
        theta = -theta          # mirror reverses rotation handedness for fields
    xr = x * math.cos(theta) - y * math.sin(theta)
    yr = x * math.sin(theta) + y * math.cos(theta)
    return float(inst_el.get('x')) + xr, float(inst_el.get('y')) + yr


# ---------------------------------------------------------------------------
# Page model
# ---------------------------------------------------------------------------

class _Page:
    """One output .kicad_sch: a top-level page (from one FRAME instance) or
    a module sheet file. Holds the IR->KiCad coordinate transform and the
    content assigned to it."""

    def __init__(self, name, fname, fx0_um, fy_top_um, width_mm, height_mm,
                 title_attrs, page_uuid):
        self.name = name
        self.fname = fname
        self.fx0 = fx0_um
        self.fy_top = fy_top_um
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.title_attrs = title_attrs   # {'title':..,'date':..,'rev':..,'company':..}
        self.uuid = page_uuid
        # HIERARCHY-INSTANCE uuid = first path segment of every symbol on this
        # page (and of module sheets under it). For the PRIMARY root and for
        # module files it equals the file uuid; a SECONDARY top-level page
        # (KiCad 10 flat multi-root, ground truth testData/t2) gets a DISTINCT
        # instance uuid, registered in .kicad_pro "sheets". Overridden in
        # export_project; defaults to the file uuid so single-root paths are
        # unaffected.
        self.path_uuid = page_uuid
        self.page_num = None             # assigned at write time
        self.body = []                   # emitted s-expr chunks (wires, symbols, ...)
        self.lib_ids = set()             # lib_ids used -> lib_symbols cache

    def pt(self, x_um, y_um):
        """IR canvas µm (Y-up) -> page mm (Y-down) — THE single Y flip."""
        return ((float(x_um) - self.fx0) / 1000,
                (self.fy_top - float(y_um)) / 1000)

    def contains(self, x_um, y_um):
        return (self.fx0 <= float(x_um) <= self.fx0 + self.width_mm * 1000
                and self.fy_top - self.height_mm * 1000
                    <= float(y_um) <= self.fy_top)


def _page_for_point(pages, x_um, y_um, label):
    hits = [p for p in pages if p.contains(x_um, y_um)]
    if len(hits) != 1:
        raise ValueError(
            f'{label}: inside {len(hits)} page frame(s), need exactly one — '
            f'every schematic object must sit on exactly one page '
            f'(same discipline as eagle_exporter sheet splitting).')
    return hits[0]


# ---------------------------------------------------------------------------
# Placed symbol
# ---------------------------------------------------------------------------

def _emit_property(lines, name, value, ax, ay, rot, size_mm, align, hide,
                   indent='\t\t'):
    # KiCad never renders property text upside down: its angle is only 0 or
    # 90. Fold 180/270 back and mirror the anchor so the text lands the same
    # place, right-side up — else a rotated symbol's Reference/Value inverts.
    rot, align = _norm_text_angle(rot, align)
    j = _justify(align)
    # Emit an explicit stroke thickness (ratio 8 = the library-field default,
    # same as export_symbol). Without it KiCad turns on auto-thickness (~15%
    # of size), rendering the instance's text noticeably bolder than the
    # library symbol it came from.
    th = size_mm * 0.08
    lines.append(f'{indent}(property {_q(name)} {_q(value)}')
    lines.append(f'{indent}\t(at {_f(ax)} {_f(ay)} {_f(rot)})')
    lines.append(f'{indent}\t(effects')
    lines.append(f'{indent}\t\t(font (size {_f(size_mm)} {_f(size_mm)}) '
                 f'(thickness {_f(th)}))' + (j if j.strip() else ''))
    if hide:
        lines.append(f'{indent}\t\t(hide yes)')
    lines.append(f'{indent}\t)')
    lines.append(f'{indent})')


def _emit_symbol_instance(page, inst_el, comp_el, pool, lib_name, proj_name,
                          ns, unit_n, gate_sym_name, sym_uuid_sink=None,
                          module_placements=None):
    """One IR component <instance> -> one placed `(symbol ...)` block.

    `module_placements` (module subsheet symbols only): a list of
    (parent_page_uuid, minst_name, offset) — one per place the owning module
    is instantiated. It turns the single canonical `(instances)` path into ONE
    path PER instance, each `/{parent}/{sheet}` (ground truth: multichannel's
    channel_strip, a subsheet instantiated 4x carries 4 paths) with that
    instance's real designator (instance_designator: numeric offset flatten or
    INST:REFDES colon composite). None/empty → the flat single-path form."""
    designator = inst_el.get('name')
    comp_name = comp_el.get('name')
    lib_id = f'{lib_name}:{comp_name}'
    page.lib_ids.add(comp_name)

    # Power/supply symbols (a `sup`-direction pin — the same test
    # export_symbol uses to mark the lib symbol `(power)`) carry no footprint
    # and must be EXCLUDED from the board: KiCad excludes any reference
    # starting with '#'. Prefix it so "Update PCB from Schematic" stops
    # demanding a footprint for +P1/GND rails (the reference is hidden anyway;
    # a supply symbol shows its Value = the net name).
    is_supply = bool(_sup_pin_names(comp_el, pool))
    ref = ('#' + designator if is_supply and not designator.startswith('#')
           else designator)

    kx, ky = page.pt(inst_el.get('x'), inst_el.get('y'))
    rot = float(inst_el.get('rot', '0')) % 360
    mirrored = inst_el.get('mirror') == '1'

    attrs = _resolved_attrs(comp_el, inst_el)
    value = _eagle_overbar_to_kicad(attrs.pop('value', '') or comp_name)
    if is_supply:
        # KiCad names the net BY THE POWER SYMBOL'S VALUE (Eagle names it
        # by the sup-pin) — anything else in Value silently renames the
        # schematic net away from the board's (caught by the closed-loop
        # oracle: Eagle "3.3V" supply device with sup-pin VDD_3V3 came
        # back as net "3.3V" on the schematic vs VDD_3V3 on the board).
        # Consistency wins over the displayed text; the visual change is
        # logged, not silent.
        sup_names = _sup_pin_names(comp_el, pool)
        if len(sup_names) == 1:
            net_name = next(iter(sup_names))
            if value != net_name:
                import_log.log(designator, value,
                               f'SUPPLY_VALUE -> "{net_name}" (KiCad names '
                               f'the net by the power symbol Value)')
                value = net_name
        else:
            import_log.log(designator, value,
                           f'SUPPLY_VALUE ambiguous: {len(sup_names)} sup '
                           f'pins, Value left as-is')

    # Footprint reference for THIS instance: per-instance variant if
    # recorded, the single footprint otherwise, '' for footprint-less.
    fps = comp_el.findall('footprint')
    inst_fp = inst_el.get('footprint')
    if inst_fp:
        # the recorded key is the footprint NAME or, for ambiguous package
        # names (one package backing several devices), the VARIANT — resolve
        # to the real package name either way
        fp_name = next((fp.get('name') for fp in fps
                        if inst_fp in (fp.get('variant'), fp.get('name'))),
                       inst_fp)
        fp_ref = f'{lib_name}:{sanitize_filename(fp_name)}'
    elif len(fps) == 1:
        fp_ref = f'{lib_name}:{sanitize_filename(fps[0].get("name", ""))}'
    else:
        fp_ref = ''
        if len(fps) > 1:
            import_log.log(designator, comp_name,
                            'FOOTPRINT_VARIANT not recorded on instance, '
                            'Footprint property left empty')

    styles = _placeholder_styles(pool[gate_sym_name])

    # Per-instance placeholder overrides (IR <instance><text> — Eagle's
    # smashed records, localized on import): a placed record moves/restyles
    # the field, a suppression-only record (hidden=yes, no geometry) hides
    # it. Without honoring these, every smashed Eagle schematic lands in
    # KiCad with fields at library-default spots (closed-loop catch:
    # tolmach is smashed nearly everywhere).
    overrides = {}
    for t in inst_el.findall('text'):
        overrides[(t.text or '').strip().lstrip('>').lower()] = t

    inst_rot = float(inst_el.get('rot', 0) or 0)
    inst_mirror = inst_el.get('mirror') == '1'

    def _override_suppressed(key):
        t = overrides.get(key.lower())
        return t is not None and t.get('hidden') == 'yes' and t.get('x') is None

    def _abs_style(key, default_dy_mm):
        t = overrides.get(key.lower())
        if t is not None and t.get('x') is not None:
            # instance-local Y-up µm -> absolute, IR canonical body
            # transform (flip local X, then rotate CCW — svg canon);
            # the record's angle is instance-relative, display angle is
            # absolute (Eagle smashed records store absolute angles,
            # eagle_parser localized them with this exact inverse)
            lx, ly = float(t.get('x', 0)), float(t.get('y', 0))
            lrot = float(t.get('rot', 0) or 0)
            if inst_mirror:
                lx = -lx
            r = math.radians(inst_rot)
            axu = float(inst_el.get('x')) + lx * math.cos(r) - ly * math.sin(r)
            ayu = float(inst_el.get('y')) + lx * math.sin(r) + ly * math.cos(r)
            ax, ay = page.pt(axu, ayu)
            arot = (inst_rot - lrot) % 360 if inst_mirror \
                else (inst_rot + lrot) % 360
            return (ax, ay, arot % 360,
                    float(t.get('size', 1778)) / 1000,
                    t.get('align', 'bottom-left'))
        if key in styles:
            lx, ly, lrot, lsize, lalign = styles[key]
            axu, ayu = _inst_point(lx, ly, inst_el)
            ax, ay = page.pt(axu, ayu)
            # Field ANGLE is the library placeholder's own angle, NOT
            # lrot + symbol rotation: KiCad does not spin field text with the
            # symbol — it keeps the text readable (library angle) and only
            # moves its position. Ground truth: a C symbol placed by KiCad at
            # rot=90 keeps Reference/Value at angle 0, position rotated.
            return ax, ay, lrot, lsize / 1000, lalign
        return kx, ky + default_dy_mm, 0, 1.27, 'center'

    u = _quuid(ns, 'sym', page.name, designator, unit_n)
    # The board footprint links to the PRIMARY unit's symbol (unit 1); record
    # its uuid so the board exporter can emit the matching (path ...) — the
    # sheet-schematic link that makes "Update PCB from Schematic" a no-op.
    if unit_n == 1 and sym_uuid_sink is not None:
        sym_uuid_sink[designator] = u
    lines = ['\t(symbol',
             f'\t\t(lib_id {_q(lib_id)})',
             f'\t\t(at {_f(kx)} {_f(ky)} {_f(rot)})']
    if mirrored:
        lines.append('\t\t(mirror y)')
    # IR has no separate bom flag (ir_schema.md "Варианты сборки" — dnp
    # already implies "not in BOM", populate=no is the only axis).
    dnp = inst_el.get('populate') == 'no'
    lines += [f'\t\t(unit {unit_n})',
              '\t\t(exclude_from_sim no)',
              f'\t\t(in_bom {"no" if dnp else "yes"})',
              '\t\t(on_board yes)',
              f'\t\t(dnp {"yes" if dnp else "no"})',
              f'\t\t(uuid "{u}")']

    ax, ay, arot, asize, aalign = _abs_style('NAME', -2.54)
    # No >NAME placeholder in the source symbol => Eagle never showed the
    # reference (pin-less parts: fiducials, screws). Hide it, exactly as Value
    # is hidden without >VALUE — "no placeholder = not displayed".
    _emit_property(lines, 'Reference', ref, ax, ay, arot, asize, aalign,
                   hide=ref.startswith('#') or 'NAME' not in styles
                        or _override_suppressed('NAME'))
    ax, ay, arot, asize, aalign = _abs_style('VALUE', 2.54)
    # Show Value only if the symbol actually has a >VALUE placeholder — an IC
    # with no value (and no >VALUE, e.g. it displays >MANF# instead) must NOT
    # sprout a Value field. Matches Eagle: no placeholder = not displayed.
    _emit_property(lines, 'Value', value, ax, ay, arot, asize, aalign,
                   hide='VALUE' not in styles or _override_suppressed('VALUE'))
    _emit_property(lines, 'Footprint', fp_ref, kx, ky, 0, 1.27, 'center', hide=True)
    _emit_property(lines, 'Datasheet', attrs.pop('datasheet', ''), kx, ky, 0,
                   1.27, 'center', hide=True)
    for k, v in attrs.items():
        st = _abs_style(k, 0)
        shown = (k in styles or k.lower() in
                 {q for q, t in overrides.items() if t.get('x') is not None
                  and t.get('hidden') != 'yes'})
        _emit_property(lines, k, _eagle_overbar_to_kicad(v), st[0], st[1],
                       st[2], st[3], st[4],
                       hide=not shown or _override_suppressed(k))

    # Per-pin uuid entries: KiCad regenerates them, kiutils reads fine
    # without — omitted (less to get wrong; ground truth carries them only
    # because real KiCad always writes what it has).

    lines += ['\t\t(instances',
              f'\t\t\t(project {_q(proj_name)}']
    if module_placements:
        for parent_uuid, minst_name, offset in module_placements:
            sheet_uuid = _quuid(ns, 'sheet', minst_name)
            per = instance_designator(designator, minst_name, offset)
            if is_supply and not per.startswith('#'):
                per = '#' + per
            lines += [f'\t\t\t\t(path "/{parent_uuid}/{sheet_uuid}"',
                      f'\t\t\t\t\t(reference {_q(per)})',
                      f'\t\t\t\t\t(unit {unit_n})',
                      '\t\t\t\t)']
    else:
        lines += [f'\t\t\t\t(path "/{page.path_uuid}"',
                  f'\t\t\t\t\t(reference {_q(ref)})',
                  f'\t\t\t\t\t(unit {unit_n})',
                  '\t\t\t\t)']
    lines += ['\t\t\t)',
              '\t\t)',
              '\t)']
    page.body.append('\n'.join(lines))


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

def _segment_page(pages, seg_el, part_page, label):
    """Which page a <segment> lives on: pinref/portref placements first,
    geometry as fallback. One page per segment, hard fail otherwise (same
    rule as eagle_exporter's net splitting)."""
    cand = {part_page[r.get('part')]
            for r in list(seg_el.findall('pinref')) + list(seg_el.findall('portref'))
            if r.get('part') in part_page}
    if len(cand) > 1:
        raise ValueError(f'net "{label}": one segment touches parts on different '
                          f'pages — a net segment must stay on one page.')
    if cand:
        return next(iter(cand))
    for w in seg_el.findall('line'):
        return _page_for_point(pages, w.get('x1'), w.get('y1'),
                               f'net "{label}" wire')
    for l in seg_el.findall('label'):
        return _page_for_point(pages, l.get('x'), l.get('y'),
                               f'net "{label}" label')
    for j in seg_el.findall('junction'):
        return _page_for_point(pages, j.get('x'), j.get('y'),
                               f'net "{label}" junction')
    return None


def _emit_wire(page, w, ns, key):
    (x1, y1) = page.pt(w.get('x1'), w.get('y1'))
    (x2, y2) = page.pt(w.get('x2'), w.get('y2'))
    width = _f(_mm(w.get('width', '0')))
    page.body.append(
        f'\t(wire\n'
        f'\t\t(pts\n'
        f'\t\t\t(xy {_f(x1)} {_f(y1)}) (xy {_f(x2)} {_f(y2)})\n'
        f'\t\t)\n'
        f'\t\t(stroke (width {width}) (type default))\n'
        f'\t\t(uuid "{_quuid(ns, "wire", key, x1, y1, x2, y2)}")\n'
        f'\t)')


def _emit_junction(page, j, ns):
    (x, y) = page.pt(j.get('x'), j.get('y'))
    page.body.append(
        f'\t(junction\n'
        f'\t\t(at {_f(x)} {_f(y)})\n'
        f'\t\t(diameter 0)\n'
        f'\t\t(color 0 0 0 0)\n'
        f'\t\t(uuid "{_quuid(ns, "junction", x, y)}")\n'
        f'\t)')


def _label_justify(rot):
    """Real KiCad pairs label angle 0/90 with `left bottom` anchoring and
    180/270 with `right bottom` (text reads unmirrored either way)."""
    return 'left bottom' if rot in (0, 90) else 'right bottom'


# IR <label> style -> KiCad global_label shape. Presentation-only on the IR
# side (ir_schema.md "<label>"), but a global_label REQUIRES a shape token;
# `crummy` (plain text) exists only for local labels -> passive.
_STYLE_SHAPE = {'input': 'input', 'output': 'output', 'bidir': 'bidirectional',
                'passive': 'passive', 'crummy': 'passive'}


def _emit_label(page, text, x_um, y_um, rot, size_um, ns, style='crummy',
                in_module=False):
    """Net-name label. THE RULE (user decision, revising the earlier blanket
    global_label ban): no global labels IN MODULES, no local labels OUTSIDE
    modules. Top-level pages are one canvas in IR — only global_label makes
    real KiCad merge same-named nets across top_level_sheets pages (local
    label scope is a single sheet file, confirmed: vimdrones' authors used
    142 globals / 0 locals for exactly this); a module is a sealed scope —
    only local labels + hierarchical ports may appear inside. This makes a
    KiCad project behave like Eagle: one namespace on the whole top level,
    modules airtight."""
    kx, ky = page.pt(x_um, y_um)
    rot = float(rot or 0) % 360
    size = _f(float(size_um or 1270) / 1000)
    if in_module:
        page.body.append(
            f'\t(label {_q(_eagle_overbar_to_kicad(text))}\n'
            f'\t\t(at {_f(kx)} {_f(ky)} {_f(rot)})\n'
            f'\t\t(effects\n'
            f'\t\t\t(font (size {size} {size}))\n'
            f'\t\t\t(justify {_label_justify(rot)})\n'
            f'\t\t)\n'
            f'\t\t(uuid "{_quuid(ns, "label", text, kx, ky)}")\n'
            f'\t)')
        return
    if style not in _STYLE_SHAPE:
        style = 'crummy'
    if style == 'crummy':
        import_log.log(text, '', 'LABEL_STYLE crummy -> global_label shape '
                        'passive (plain text exists only on local labels)')
    # Ground truth (testData/vimdrones, real KiCad 9 output): justify is
    # bare left/right (vertical centering on the wire is implied);
    # Intersheetrefs property is KiCad's own decoration, not required.
    page.body.append(
        f'\t(global_label {_q(_eagle_overbar_to_kicad(text))}\n'
        f'\t\t(shape {_STYLE_SHAPE[style]})\n'
        f'\t\t(at {_f(kx)} {_f(ky)} {_f(rot)})\n'
        f'\t\t(fields_autoplaced yes)\n'
        f'\t\t(effects\n'
        f'\t\t\t(font (size {size} {size}))\n'
        f'\t\t\t(justify {"left" if rot in (0, 90) else "right"})\n'
        f'\t\t)\n'
        f'\t\t(uuid "{_quuid(ns, "label", text, kx, ky)}")\n'
        f'\t)')


def _wires_um(seg_el):
    """A segment's wires as ((x1,y1),(x2,y2)) integer-µm tuples."""
    return [((int(float(w.get('x1'))), int(float(w.get('y1')))),
             (int(float(w.get('x2'))), int(float(w.get('y2')))))
            for w in seg_el.findall('line')]


def _label_anchor(net_name, segments, pages, part_page, other_wires):
    """A SAFE point on this net's own wires to anchor a synthesized label /
    hierarchical label: endpoints and midpoints of the net's wires, first
    one that does NOT lie on any OTHER net's wire. "First wire endpoint"
    without this check silently MERGED two nets (found on the user-edited
    multichannel: the synthesized "N$2" label landed exactly on an
    unrelated net's wire crossing that point — a KiCad label attaches to
    every wire passing through it). Returns (page, x_um, y_um) or None.
    """
    fallback = None
    for seg_el in segments:
        wires = _wires_um(seg_el)
        if not wires:
            continue
        page = _segment_page(pages, seg_el, part_page, net_name)
        if page is None:
            continue
        for a, b in wires:
            mid = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
            for pt in (a, b, mid):
                if fallback is None:
                    fallback = (page, pt)
                if not any(_collinear_between(wa, wb, pt)
                           for wa, wb in other_wires):
                    return page, pt[0], pt[1]
    if fallback is not None:
        import_log.log(net_name, '', 'LABEL_ANCHOR every point of the net '
                        'touches another net\'s wire — label may merge nets, '
                        'check in KiCad')
        return fallback[0], fallback[1][0], fallback[1][1]
    return None


def _emit_nets(pages, canvas_el, part_page, comp_by_desig, pool, ns,
               hier_ports=None):
    """All <net> content of one IR canvas onto its page(s).

    hier_ports: {net_name: (direction, label_or_None)} for a module canvas —
    the port's hierarchical_label REPLACES that net's ordinary label (plan
    §4: связность гарантирована, метка замещается). Its presence also marks
    this canvas as a MODULE: labels are emitted local inside a module,
    global on the top level (see _emit_label for the rule)."""
    in_module = hier_ports is not None
    hier_ports = hier_ports or {}
    consumed_hier = set()

    # Full wire geometry per net — collision index for _label_anchor.
    wires_by_net = {n.get('name'): [w for s in n.findall('segment')
                                    for w in _wires_um(s)]
                    for n in canvas_el.findall('net')}

    for net_el in canvas_el.findall('net'):
        net_name = net_el.get('name')
        segments = net_el.findall('segment')

        has_label = any(seg.findall('label') for seg in segments)
        self_named = any(
            comp_by_desig.get(r.get('part')) is not None
            and net_name in _sup_pin_names(comp_by_desig[r.get('part')], pool)
            for seg in segments for r in seg.findall('pinref'))

        # Which single label position the hierarchical label takes over
        # (first label of the net; for a label-less net — first wire end).
        hier = hier_ports.get(net_name)
        hier_placed = False

        for seg_el in segments:
            page = _segment_page(pages, seg_el, part_page, net_name)
            if page is None:
                import_log.log(net_name, '', 'NET_SEGMENT has no geometry and '
                                'no placed refs, skipped')
                continue
            for w in seg_el.findall('line'):
                _emit_wire(page, w, ns, net_name)
            for j in seg_el.findall('junction'):
                _emit_junction(page, j, ns)
            for l in seg_el.findall('label'):
                if hier and not hier_placed:
                    _emit_hier_label(page, net_name, hier[0],
                                     l.get('x'), l.get('y'),
                                     l.get('rot', '0'), ns)
                    hier_placed = True
                    consumed_hier.add(net_name)
                    continue
                _emit_label(page, net_name, l.get('x'), l.get('y'),
                            l.get('rot', '0'), l.get('size', '1270'), ns,
                            style=l.get('style', 'crummy'), in_module=in_module)

        # Hierarchical label for a port whose net had no label of its own
        # (synthesized supply ports): safe anchor on the net's own wires
        # (see _label_anchor for why not just "first wire endpoint").
        if hier and not hier_placed:
            other = [w for nm, ws in wires_by_net.items()
                     if nm != net_name for w in ws]
            anchor = _label_anchor(net_name, segments, pages, part_page, other)
            if anchor is not None:
                page, ax, ay = anchor
                _emit_hier_label(page, net_name, hier[0], ax, ay, '0', ns)
                hier_placed = True
                consumed_hier.add(net_name)
            else:
                import_log.log(net_name, '', 'MODULE_PORT net has no wire to '
                                'anchor a hierarchical_label, port left dangling')

        # Label synthesis (plan §3): a user-named net with no label of its
        # own and no supply self-naming loses its name in KiCad — emit one
        # local label at the first wire end. N$-autonames are NOT
        # materialized — except when the net carries a class= (the
        # netclass_pattern in .kicad_pro matches by exact name, an unnamed
        # net would silently lose its class — logged).
        needs_name = (not has_label and not self_named and not hier
                      and (not re.fullmatch(r'N\$\d+', net_name)
                           or net_el.get('class')))
        if needs_name:
            other = [w for nm, ws in wires_by_net.items()
                     if nm != net_name for w in ws]
            anchor = _label_anchor(net_name, segments, pages, part_page, other)
            if anchor is not None:
                page, ax, ay = anchor
                _emit_label(page, net_name, ax, ay, '0', '1270', ns,
                            in_module=in_module)
                if re.fullmatch(r'N\$\d+', net_name):
                    import_log.log(net_name, '', 'NET_LABEL synthesized for an '
                                    'auto-named net to keep its class assignment')
            else:
                import_log.log(net_name, '', 'NET name has no wire to anchor a '
                                'label, name will be lost in KiCad')

    for pname in hier_ports:
        if pname not in consumed_hier:
            import_log.log(pname, '', 'MODULE_PORT has no matching inner net, '
                            'no hierarchical_label emitted')


def _emit_hier_label(page, name, direction, x_um, y_um, rot, ns):
    kx, ky = page.pt(x_um, y_um)
    rot = float(rot or 0) % 360
    if direction == 'pwr':
        import_log.log(name, '', 'PORT_DIRECTION pwr -> hierarchical_label '
                        'shape passive (KiCad has no power shape)')
    shape = _PORT_SHAPE.get(direction, 'bidirectional')
    page.body.append(
        f'\t(hierarchical_label {_q(_eagle_overbar_to_kicad(name))}\n'
        f'\t\t(shape {shape})\n'
        f'\t\t(at {_f(kx)} {_f(ky)} {_f(rot)})\n'
        f'\t\t(effects\n'
        f'\t\t\t(font (size 1.27 1.27))\n'
        f'\t\t\t(justify {_label_justify(rot)})\n'
        f'\t\t)\n'
        f'\t\t(uuid "{_quuid(ns, "hlabel", name, kx, ky)}")\n'
        f'\t)')


# ---------------------------------------------------------------------------
# Decorative geometry (ir_schema.md "Декоративная геометрия схемы") + <note>
# ---------------------------------------------------------------------------

def _emit_deco(page, el, ns):
    t = el.tag
    if t == 'line':
        (x1, y1) = page.pt(el.get('x1'), el.get('y1'))
        (x2, y2) = page.pt(el.get('x2'), el.get('y2'))
        w = _f(_mm(el.get('width', '0')))
        page.body.append(
            f'\t(polyline\n'
            f'\t\t(pts\n'
            f'\t\t\t(xy {_f(x1)} {_f(y1)}) (xy {_f(x2)} {_f(y2)})\n'
            f'\t\t)\n'
            f'\t\t(stroke (width {w}) (type default))\n'
            f'\t\t(uuid "{_quuid(ns, "deco", x1, y1, x2, y2)}")\n'
            f'\t)')
    elif t == 'shape':
        cx, cy = page.pt(el.get('x'), el.get('y'))
        w2 = float(el.get('w', '0')) / 2000
        h2 = float(el.get('h', '0')) / 2000
        outline = _f(_mm(el.get('outline', '0')))
        fill = 'none' if float(el.get('outline', '0')) else 'color'
        if int(el.get('roundness', 0)) == 100:
            page.body.append(
                f'\t(circle\n'
                f'\t\t(center {_f(cx)} {_f(cy)})\n'
                f'\t\t(radius {_f(w2)})\n'
                f'\t\t(stroke (width {outline}) (type default))\n'
                f'\t\t(fill (type {fill}))\n'
                f'\t\t(uuid "{_quuid(ns, "deco-c", cx, cy)}")\n'
                f'\t)')
        else:
            page.body.append(
                f'\t(rectangle\n'
                f'\t\t(start {_f(cx - w2)} {_f(cy - h2)})\n'
                f'\t\t(end {_f(cx + w2)} {_f(cy + h2)})\n'
                f'\t\t(stroke (width {outline}) (type default))\n'
                f'\t\t(fill (type {fill}))\n'
                f'\t\t(uuid "{_quuid(ns, "deco-r", cx, cy)}")\n'
                f'\t)')
    elif t == 'arc':
        # endpoint canon: endpoints verbatim, mid derived in Y-up IR space,
        # each point then mapped through the page transform (the Y flip
        # implicitly reverses the sweep direction, matching how the importer
        # derived the curve via _arc_params on negated-Y points).
        x1, y1 = float(el.get('x1')), float(el.get('y1'))
        x2, y2 = float(el.get('x2')), float(el.get('y2'))
        mxy = arc_mid(x1, y1, x2, y2, float(el.get('curve')))
        (sx, sy), (mx, my), (ex, ey) = (page.pt(x1, y1), page.pt(*mxy),
                                        page.pt(x2, y2))
        w = _f(_mm(el.get('width', '0')))
        page.body.append(
            f'\t(arc\n'
            f'\t\t(start {_f(sx)} {_f(sy)})\n'
            f'\t\t(mid {_f(mx)} {_f(my)})\n'
            f'\t\t(end {_f(ex)} {_f(ey)})\n'
            f'\t\t(stroke (width {w}) (type default))\n'
            f'\t\t(fill (type none))\n'
            f'\t\t(uuid "{_quuid(ns, "deco-a", sx, sy, ex, ey)}")\n'
            f'\t)')
    elif t == 'text':
        kx, ky = page.pt(el.get('x'), el.get('y'))
        size = _f(float(el.get('size', '1270')) / 1000)
        rot = float(el.get('rot', '0')) % 360
        j = _justify(el.get('align', 'bottom-left'))
        page.body.append(
            f'\t(text {_qt(_eagle_overbar_to_kicad(el.text or ""))}\n'
            f'\t\t(exclude_from_sim no)\n'
            f'\t\t(at {_f(kx)} {_f(ky)} {_f(rot)})\n'
            f'\t\t(effects\n'
            f'\t\t\t(font (size {size} {size})){j}\n'
            f'\t\t)\n'
            f'\t\t(uuid "{_quuid(ns, "deco-t", kx, ky)}")\n'
            f'\t)')
    elif t == 'note':
        # <note> -> (text_box): x/y = top-left, w from the note; height is an
        # ESTIMATE (line count x 1.6 x font size, min 10mm) — IR stores no
        # box height, our renderer wraps with its own metrics (plan §6;
        # symmetric to the "переносы руками" decision on import).
        kx, ky = page.pt(el.get('x'), el.get('y'))
        w_mm = float(el.get('w', '0')) / 1000 or 40
        md = el.text or ''
        n_lines = max(md.count('\n') + 1, 1)
        h_mm = max(n_lines * 1.6 * 1.27, 10)
        rot = float(el.get('rot', '0')) % 360
        page.body.append(
            f'\t(text_box {_qt(md)}\n'
            f'\t\t(exclude_from_sim no)\n'
            f'\t\t(at {_f(kx)} {_f(ky)} {_f(rot)})\n'
            f'\t\t(size {_f(w_mm)} {_f(h_mm)})\n'
            f'\t\t(stroke (width 0) (type default))\n'
            f'\t\t(fill (type none))\n'
            f'\t\t(effects\n'
            f'\t\t\t(font (size 1.27 1.27))\n'
            f'\t\t\t(justify left top)\n'
            f'\t\t)\n'
            f'\t\t(uuid "{_quuid(ns, "note", kx, ky)}")\n'
            f'\t)')


# ---------------------------------------------------------------------------
# Sheet blocks (module instances on a parent page)
# ---------------------------------------------------------------------------

def _port_pin_pos(x0_mm, y0_mm, w_mm, h_mm, side, coord_um):
    """Inverse of kicad_project_parser._sheet_ports: side + signed offset
    from the block CENTER (µm, +up on vertical edges / +right on horizontal)
    -> absolute sheet-space pin position (mm, Y-down)."""
    coord = float(coord_um) / 1000
    cx, cy = x0_mm + w_mm / 2, y0_mm + h_mm / 2
    if side == 'left':
        return x0_mm, cy - coord
    if side == 'right':
        return x0_mm + w_mm, cy - coord
    if side == 'top':
        return cx + coord, y0_mm
    return cx + coord, y0_mm + h_mm


def _emit_sheet(page, inst_el, mod_el, mod_page, proj_name, ns, page_num):
    """One <instance module=...> -> one (sheet ...) block. The instance's
    own rot/mirror is BAKED into this occurrence's layout (KiCad sheets
    carry no angle/mirror of their own — see ir_util.rotate_port_side):
    dx/dy swap for a 90/270 rotation (block center fixed, same as the
    ground-truth fixture), and every port's side/coord is transformed the
    same way real KiCad relaid them out when the user rotated/mirrored a
    sheet by hand."""
    inst_name = inst_el.get('name')
    stem = mod_el.get('name')
    rot_deg = float(inst_el.get('rot', '0'))
    mirror = inst_el.get('mirror') == '1'
    dx_mm = float(mod_el.get('dx')) / 1000
    dy_mm = float(mod_el.get('dy')) / 1000
    swapped = round(rot_deg) % 180 == 90
    w_mm, h_mm = (dy_mm, dx_mm) if swapped else (dx_mm, dy_mm)
    kcx, kcy = page.pt(inst_el.get('x'), inst_el.get('y'))
    x0, y0 = kcx - w_mm / 2, kcy - h_mm / 2
    u = _quuid(ns, 'sheet', inst_name)

    lines = ['\t(sheet',
             f'\t\t(at {_f(x0)} {_f(y0)})',
             f'\t\t(size {_f(w_mm)} {_f(h_mm)})',
             '\t\t(exclude_from_sim no)',
             '\t\t(in_bom yes)',
             '\t\t(on_board yes)',
             '\t\t(dnp no)',
             '\t\t(fields_autoplaced yes)',
             '\t\t(stroke (width 0.1524) (type solid))',
             '\t\t(fill (color 0 0 0 0))',
             f'\t\t(uuid "{u}")']
    _emit_property(lines, 'Sheetname', inst_name, x0, y0 - 0.7, 0, 1.27,
                   'bottom-left', hide=False)
    # Sheetfile MUST be the module page's real (collision-resolved) filename,
    # not a fresh sanitize(stem) — when the module name equals the project (or
    # a top page) name the module file was renamed to avoid clobbering it.
    _emit_property(lines, 'Sheetfile', mod_page.fname,
                   x0, y0 + h_mm + 0.6, 0, 1.27, 'top-left', hide=False)
    for p in mod_el.findall('port'):
        side, coord_um = rotate_port_side(
            p.get('side'), float(p.get('coord', '0')), rot_deg, mirror)
        px, py = _port_pin_pos(x0, y0, w_mm, h_mm, side, coord_um)
        shape = _PORT_SHAPE.get(p.get('direction', 'io'), 'bidirectional')
        pj = {'right': 'right', 'left': 'left',
              'top': 'left', 'bottom': 'left'}[side]
        lines += [f'\t\t(pin {_q(p.get("name"))} {shape}',
                  f'\t\t\t(at {_f(px)} {_f(py)} {_SIDE_ANGLE[side]})',
                  f'\t\t\t(uuid "{_quuid(ns, "sheetpin", inst_name, p.get("name"))}")',
                  '\t\t\t(effects',
                  '\t\t\t\t(font (size 1.27 1.27))',
                  f'\t\t\t\t(justify {pj})',
                  '\t\t\t)',
                  '\t\t)']
    lines += ['\t\t(instances',
              f'\t\t\t(project {_q(proj_name)}',
              f'\t\t\t\t(path "/{page.path_uuid}"',
              f'\t\t\t\t\t(page "{page_num}")',
              '\t\t\t\t)',
              '\t\t\t)',
              '\t\t)',
              '\t)']
    page.body.append('\n'.join(lines))


# ---------------------------------------------------------------------------
# Canvas -> page(s)
# ---------------------------------------------------------------------------

def _split_canvas_instances(canvas_el, comp_by_name, pool):
    """(frame_insts, part_insts, module_insts) of one IR canvas."""
    frames, parts, mods = [], [], []
    for inst_el in canvas_el.findall('instance'):
        if inst_el.get('module'):
            mods.append(inst_el)
            continue
        comp_el = comp_by_name.get(inst_el.get('component'))
        if comp_el is None:
            import_log.log(inst_el.get('name'), inst_el.get('component'),
                            'INSTANCE component not found in pool, dropped')
            continue
        (frames if _is_frame_component(comp_el, pool) else parts).append(inst_el)
    return frames, parts, mods


def _pages_from_frames(canvas_el, frame_insts, comp_by_name, pool, name_fn, ns):
    """FRAME instances -> [_Page], left-to-right then top-to-bottom (the
    import tiling is a horizontal row, so X is the primary order)."""
    pages = []
    boxes = sorted(
        ((_frame_bbox(i, comp_by_name[i.get('component')], pool), i)
         for i in frame_insts),
        key=lambda b: (b[0][0], -b[0][3]))
    for idx, ((x1, x2, y1, y2), inst_el) in enumerate(boxes):
        comp_el = comp_by_name[inst_el.get('component')]
        attrs = _resolved_attrs(comp_el, inst_el)
        title = {k: attrs.get(k, '') for k in ('title', 'date', 'rev', 'company')}
        pname, fname = name_fn(idx)
        pages.append(_Page(pname, fname, x1, y2,
                           (x2 - x1) / 1000, (y2 - y1) / 1000, title,
                           _quuid(ns, 'page', pname)))
    if not pages:
        # No frames at all: one page sized to the content bbox (plan: bbox
        # -> nearest ISO or User + warning).
        xs, ys = [0.0], [0.0]
        for el in canvas_el.iter():
            for ax, ay in (('x', 'y'), ('x1', 'y1'), ('x2', 'y2')):
                if el.get(ax) is not None and el.get(ay) is not None:
                    xs.append(float(el.get(ax)))
                    ys.append(float(el.get(ay)))
        w_mm = max(xs) / 1000 + 20
        h_mm = -min(ys) / 1000 + 20
        for iso_w, iso_h in sorted((h, w) for w, h in _ISO_PAPER_MM.values()):
            if w_mm <= iso_w and h_mm <= iso_h:
                w_mm, h_mm = iso_w, iso_h
                break
        print(f'  ! no page frame in the source IR — single page '
              f'{w_mm:g}x{h_mm:g}mm from content bbox')
        pname, fname = name_fn(0)
        pages.append(_Page(pname, fname, min(xs), 0, w_mm, h_mm,
                           {}, _quuid(ns, 'page', pname)))
    return pages


def _emit_canvas(pages, canvas_el, part_insts, comp_by_name, pool, lib_name,
                 proj_name, ns, hier_ports=None, sym_uuid_sink=None,
                 module_placements=None):
    """Everything except sheets: placed symbols, nets, deco, notes.

    `module_placements` (module canvas only) is forwarded to every symbol so
    each carries one `(instances)` path per module instantiation."""
    part_page = {}
    comp_by_desig = {}
    for inst_el in part_insts:
        comp_el = comp_by_name[inst_el.get('component')]
        page = _page_for_point(pages, inst_el.get('x'), inst_el.get('y'),
                               inst_el.get('name'))
        part_page[inst_el.get('name')] = page
        comp_by_desig[inst_el.get('name')] = comp_el

        gates = component_gates(comp_el)
        gate = inst_el.get('gate')
        if gate is None:
            unit_n, gate_sym = 1, gates[0][1]
        else:
            letters = [g[0] for g in gates]
            if gate not in letters:
                raise ValueError(f'{inst_el.get("name")}: gate "{gate}" not '
                                  f'defined on component {comp_el.get("name")}')
            unit_n = letters.index(gate) + 1
            gate_sym = gates[letters.index(gate)][1]
        _emit_symbol_instance(page, inst_el, comp_el, pool, lib_name,
                              proj_name, ns, unit_n, gate_sym,
                              sym_uuid_sink=sym_uuid_sink,
                              module_placements=module_placements)

    for el in canvas_el:
        if el.tag in ('line', 'arc', 'shape', 'text', 'note'):
            if el.tag in ('line', 'arc'):
                px, py = el.get('x1'), el.get('y1')
            else:
                px, py = el.get('x'), el.get('y')
            _emit_deco(_page_for_point(pages, px, py, f'deco {el.tag}'), el, ns)

    return part_page, comp_by_desig


# ---------------------------------------------------------------------------
# File assembly
# ---------------------------------------------------------------------------

def _lib_symbols_cache(lib_ids, comp_by_name, ir_root, lib_name,
                       power_local=False):
    """(lib_symbols ...) block: READY symbol s-exprs from export_symbol
    (format reuse, plan "Реюз"), renamed to their full `<nick>:<name>`
    lib_id — only the OUTER symbol name carries the nickname (ground truth:
    sub-unit names stay bare).

    power_local=True (module sheet files): supply symbols' cache copies get
    `(power local)` instead of `(power global)` — KiCad 10 scopes the
    supply net to this one sheet file, which is EXACTLY the IR module
    semantics (nothing leaks, ports are the only link; the port itself is a
    real sheet pin, so connectivity stays intact). Locality lives on the
    per-file cache copy (ground truth: user-saved channel_strip.kicad_sch —
    KiCad forks the copy per file), so a uniform per-file flip needs no
    GND_1-style fork at all. Top-level pages keep `(power global)`:
    cross-page merge by name is the IR single-canvas semantics."""
    chunks = ['\t(lib_symbols']
    for comp_name in sorted(lib_ids):
        comp_el = comp_by_name.get(comp_name)
        if comp_el is None:
            continue
        sexp = export_symbol(comp_el, ir_root, lib_name)
        if not sexp:
            continue
        old = f'(symbol {_q(comp_name)}'
        new = f'(symbol {_q(f"{lib_name}:{comp_name}")}'
        sexp = sexp.replace(old, new, 1)
        if power_local:
            sexp = sexp.replace('(power global)', '(power local)', 1)
        chunks.append('\n'.join('\t' + line for line in sexp.split('\n')))
    chunks.append('\t)')
    return '\n'.join(chunks)


def _write_page(page, out_dir, comp_by_name, ir_root, lib_name,
                is_module=False):
    tb = page.title_attrs or {}
    paper, warn = _paper_from_frame(page.width_mm, page.height_mm)
    if warn:
        print(f'  ! {page.fname}: {warn}')
    parts = ['(kicad_sch',
             f'\t(version {_SCH_VERSION})',
             f'\t(generator "{_GENERATOR}")',
             '\t(generator_version "1.0")',
             f'\t(uuid "{page.uuid}")',
             f'\t{paper}']
    tb_lines = [f'\t\t({k} {_q(v)})' for k, v in
                (('title', tb.get('title')), ('date', tb.get('date')),
                 ('rev', tb.get('rev')), ('company', tb.get('company')))
                if v]
    if tb_lines:
        parts.append('\t(title_block\n' + '\n'.join(tb_lines) + '\n\t)')
    parts.append(_lib_symbols_cache(page.lib_ids, comp_by_name, ir_root, lib_name,
                                    power_local=is_module))
    parts.extend(page.body)
    if not is_module:
        # Only an independent top-level root carries its own
        # `(sheet_instances (path "/"))` — a module file's page number lives
        # on the parent's (sheet)-instances block instead (ground truth:
        # channel_strip.kicad_sch has no sheet_instances at all).
        parts.append(f'\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "{page.page_num}")\n\t\t)\n\t)')
    parts.append('\t(embedded_fonts no)')
    parts.append(')')
    (out_dir / page.fname).write_text('\n'.join(parts) + '\n', encoding='utf-8')


def _write_kicad_pro(out_dir, proj_name, ir_root, top_pages, class_patterns,
                     sheet_registry=None):
    # Schematic wire/bus default for every net class. UNITS TRAP (ground
    # truth freq/multigate .kicad_pro): net-class SCHEMATIC widths are in
    # MILS (wire_width 6, bus_width 12) while the PCB widths in the same JSON
    # are in mm (track_width 0.2). A class without wire_width renders wires —
    # and the junction dots sized from them — at zero width, i.e. invisible.
    _WIRE_MIL, _BUS_MIL = 6, 12
    classes = [{
        'name': 'Default', 'priority': 2147483647,
        'clearance': 0.2, 'track_width': 0.2,
        'via_diameter': 0.6, 'via_drill': 0.3,
        'wire_width': _WIRE_MIL, 'bus_width': _BUS_MIL,
    }]
    ir_classes = ir_root.find('classes')
    if ir_classes is not None:
        for i, cl in enumerate(ir_classes.findall('class')):
            c = {'name': cl.get('name'), 'priority': i,
                 'wire_width': _WIRE_MIL, 'bus_width': _BUS_MIL}
            for src, dst in (('width', 'track_width'), ('drill', 'via_drill'),
                              ('clearance', 'clearance')):
                if cl.get(src) is not None:
                    c[dst] = float(cl.get(src)) / 1000
            classes.append(c)

    pro = {
        'meta': {'filename': f'{proj_name}.kicad_pro', 'version': 3},
        'net_settings': {
            'meta': {'version': 5},
            'classes': classes,
            'netclass_patterns': class_patterns,
        },
        # WITHOUT a schematic.drawing block KiCad renders wires and junction
        # dots at zero size (invisible), regardless of the net-class
        # wire_width. default_line_thickness (6 mil) is what actually sizes
        # schematic wires; junction_size_choice=3 is KiCad's normal dot size.
        # Ground truth: multigate.kicad_pro. KiCad fills the rest of the
        # schematic section with defaults on open.
        'schematic': {
            'drawing': {
                'dashed_lines_dash_length_ratio': 12.0,
                'dashed_lines_gap_length_ratio': 3.0,
                'default_line_thickness': 6.0,
                'default_text_size': 50.0,
                'field_names': [],
                'intersheets_ref_own_page': False,
                'intersheets_ref_prefix': '',
                'intersheets_ref_short': False,
                'intersheets_ref_show': False,
                'intersheets_ref_suffix': '',
                'junction_size_choice': 3,
                'label_size_ratio': 0.375,
                'overbar_offset_ratio': 1.23,
                'pin_symbol_size': 25.0,
                'text_offset_ratio': 0.15,
            },
        },
    }

    # The six-number DRC core (ir_schema.md "DRC-ядро") -> KiCad
    # design_settings.rules (key names ground truth: Pocket-Lab .kicad_pro)
    rules_el = ir_root.find('layout/rules')
    if rules_el is not None:
        mm = lambda a: float(rules_el.get(a)) / 1000
        rules = {}
        for attr, key in (('clearance', 'min_clearance'),
                          ('edge_clearance', 'min_copper_edge_clearance'),
                          ('min_width', 'min_track_width'),
                          ('min_drill', 'min_through_hole_diameter'),
                          ('min_annular', 'min_via_annular_width'),
                          ('min_drill_web', 'min_hole_to_hole')):
            if rules_el.get(attr):
                rules[key] = mm(attr)
        if rules_el.get('min_drill') and rules_el.get('min_annular'):
            rules['min_via_diameter'] = mm('min_drill') + 2 * mm('min_annular')
        pro['board'] = {'design_settings': {'rules': rules}}
    # KiCad 10 flat multi-root (ground truth testData/t2) uses BOTH fields:
    #  - top_level_sheets: [{filename, name, uuid}] — one per top-level page;
    #    its `uuid` is the page's HIERARCHY-INSTANCE uuid (file uuid for the
    #    primary root, the distinct instance uuid for a secondary top). This
    #    is what makes KiCad DISCOVER and LOAD the p2/p3 files at all.
    #  - sheets: [[instance_uuid, name]] — the flat inventory of EVERY sheet
    #    instance (top pages + every module instance), how KiCad numbers/
    #    navigates the whole hierarchy.
    # Emitting only one leaves pages invisible (sheets alone) or the module
    # instances unregistered (top_level_sheets alone). Both, or neither for a
    # lone flat page (KiCad fills the single-root form on open).
    if len(top_pages) > 1:
        pro['schematic']['top_level_sheets'] = [
            {'filename': p.fname, 'name': p.name, 'uuid': p.path_uuid}
            for p in top_pages]
    (out_dir / f'{proj_name}.kicad_pro').write_text(
        json.dumps(pro, indent=2), encoding='utf-8')


def _write_lib_tables(out_dir, lib_name, have_pretty):
    (out_dir / 'sym-lib-table').write_text(
        '(sym_lib_table\n\t(version 7)\n'
        f'\t(lib (name {_q(lib_name)}) (type "KiCad") '
        f'(uri "${{KIPRJMOD}}/{lib_name}.kicad_sym") (options "") (descr ""))\n)\n',
        encoding='utf-8')
    fp_entry = (f'\t(lib (name {_q(lib_name)}) (type "KiCad") '
                f'(uri "${{KIPRJMOD}}/{lib_name}.pretty") (options "") (descr ""))\n'
                if have_pretty else '')
    (out_dir / 'fp-lib-table').write_text(
        f'(fp_lib_table\n\t(version 7)\n{fp_entry})\n', encoding='utf-8')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def export_project(ir_path, output_dir):
    """IR <project> (.swprj) -> a full self-contained KiCad project in
    output_dir: .kicad_pro + top-level page .kicad_sch files + one
    .kicad_sch per <module> + .kicad_sym/.pretty + lib tables. Returns the
    .kicad_pro path."""
    ir_path = Path(ir_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ir_root = ET.parse(ir_path).getroot()
    if ir_root.find('schematic') is None:
        raise ValueError(f'{ir_path}: no <schematic> section — use '
                          f'kicad_exporter.export() for a library-only IR.')
    proj_name = sanitize_filename(ir_root.get('name', ir_path.stem))
    lib_name = proj_name
    ns = uuid.uuid5(uuid.NAMESPACE_URL, f'babel:{proj_name}')

    pool = symbol_pool(ir_root)
    comp_by_name = {c.get('name'): c for c in ir_root.findall('component')}
    schem_el = ir_root.find('schematic')
    module_els = ir_root.findall('module')

    # --- Library part (plan §7): .kicad_sym + .pretty via the existing
    # library exporter; frame components excluded (they become paper/
    # title_block, never library symbols).
    frame_comp_names = {c.get('name') for c in ir_root.findall('component')
                        if _is_frame_component(c, pool)}
    export_library(ir_path, out_dir, lib_name=lib_name,
                   skip_components=frame_comp_names)
    _write_lib_tables(out_dir, lib_name,
                      have_pretty=any((out_dir / f'{lib_name}.pretty').iterdir()))

    # --- Top-level pages from FRAME instances.
    frame_insts, part_insts, module_insts = _split_canvas_instances(
        schem_el, comp_by_name, pool)

    def _top_name(idx):
        if idx == 0:
            return proj_name, f'{proj_name}.kicad_sch'
        return f'{proj_name}_p{idx + 1}', f'{proj_name}_p{idx + 1}.kicad_sch'

    top_pages = _pages_from_frames(schem_el, frame_insts, comp_by_name, pool,
                                   _top_name, ns)
    for i, p in enumerate(top_pages):
        p.page_num = i + 1
        # KiCad 10 flat multi-root (ground truth testData/t2): the primary
        # root's hierarchy-instance uuid IS its file uuid; every SECONDARY
        # top-level page gets a distinct instance uuid (registered in
        # .kicad_pro "sheets"), used as the first path segment of its symbols
        # and of the module sheets placed on it. Single-page projects keep
        # the file uuid (i == 0), so nothing changes for them.
        if i > 0:
            p.path_uuid = _quuid(ns, 'topinst', p.name)

    # --- Pre-pass: place every module instance on its parent page BEFORE the
    # subsheet symbols are emitted, so each symbol can carry one (instances)
    # path per instantiation (parent page uuid + this instance's sheet uuid +
    # its real designator). Placement/validation only touches top_pages, not
    # the module pages built below — no ordering cycle.
    modinst_table = {}   # stem -> [(parent_page_path_uuid, minst_name, offset), ...]
    modinst_page = {}    # minst_name -> parent _Page (reused by the sheet loop)
    modinst_pagenum = {} # minst_name -> global page number (each instance distinct)
    next_pn = len(top_pages) + 1
    for inst_el in module_insts:
        stem = inst_el.get('module')
        if stem not in {m.get('name') for m in module_els}:
            raise ValueError(f'{inst_el.get("name")}: module "{stem}" not '
                              f'defined in this IR.')
        if round(float(inst_el.get('rot', '0'))) % 90:
            raise ValueError(f'{inst_el.get("name")}: module instance rot='
                              f'{inst_el.get("rot")} is not a multiple of 90 '
                              f'— KiCad sheets only support axis-aligned '
                              f'rotation.')
        # offset-less module instance -> its designators keep the Eagle
        # INST:REFDES colon form (TM1:R6). KiCad accepts a colon in a refdes
        # (verified in real KiCad 10 — the earlier "needs annotation" symptom
        # was the empty top_level_sheets, not the colon), so the prefix model
        # works as-is; no reject.
        page = _page_for_point(top_pages, inst_el.get('x'), inst_el.get('y'),
                               inst_el.get('name'))
        modinst_page[inst_el.get('name')] = page
        modinst_pagenum[inst_el.get('name')] = next_pn
        next_pn += 1
        # parent uuid = the page's HIERARCHY-INSTANCE uuid (file uuid for the
        # primary root, the distinct instance uuid for a secondary top page)
        modinst_table.setdefault(stem, []).append(
            (page.path_uuid, inst_el.get('name'), inst_el.get('offset')))

    # --- Module pages (one file per module definition).
    mod_page_by_stem = {}
    mod_el_by_stem = {}
    mod_sym_uuids = {}   # stem -> {canonical designator -> module symbol uuid}
    used_fnames = {p.fname for p in top_pages}   # reserve top-page filenames
    next_page_num = len(top_pages) + 1
    for mod_el in module_els:
        stem = mod_el.get('name')
        mod_el_by_stem[stem] = mod_el
        m_frames, m_parts, m_mods = _split_canvas_instances(
            mod_el, comp_by_name, pool)
        if m_mods:
            raise ValueError(f'module "{stem}": nested module instance — '
                              f'depth >= 2 is a hard reject (ir_schema.md '
                              f'"Модуль": глубина ровно 1).')

        # Module file name must not collide with a top-level page file (or
        # another module) — happens when the module is named after the project
        # (project NAMUR + module NAMUR would both want NAMUR.kicad_sch, and
        # the module, written last, would clobber top page 1). Keep the
        # Sheetname = stem; only the FILE gets a suffix.
        _base = sanitize_filename(stem)
        _fn = f'{_base}.kicad_sch'
        if _fn in used_fnames:
            _n = 1
            while f'{_base}_mod{_n}.kicad_sch' in used_fnames:
                _n += 1
            _fn = f'{_base}_mod{_n}.kicad_sch'
        used_fnames.add(_fn)

        def _mod_name(idx, _stem=stem, _f=_fn):
            return _stem, _f

        m_pages = _pages_from_frames(mod_el, m_frames, comp_by_name, pool,
                                     _mod_name, ns)
        if len(m_pages) != 1:
            raise ValueError(f'module "{stem}": {len(m_pages)} page frames — '
                              f'a module is exactly one sheet file.')
        mpage = m_pages[0]
        mpage.page_num = next_page_num   # provisional; per-instance pages
        next_page_num += 1               # get distinct numbers below
        mod_page_by_stem[stem] = mpage

        hier_ports = {p.get('name'): (p.get('direction', 'io'), None)
                      for p in mod_el.findall('port')}
        # capture {canonical designator -> module symbol uuid} so the board
        # can link each module-instance footprint (below)
        mod_sym_uuids[stem] = {}
        part_page, comp_by_desig = _emit_canvas(
            m_pages, mod_el, m_parts, comp_by_name, pool, lib_name,
            proj_name, ns, hier_ports=hier_ports,
            module_placements=modinst_table.get(stem, []),
            sym_uuid_sink=mod_sym_uuids[stem])
        _emit_nets(m_pages, mod_el, part_page, comp_by_desig, pool, ns,
                   hier_ports=hier_ports)

    # --- Top canvas content. sym_uuids collects primary-unit symbol uuids
    # so the board footprints can carry the matching (path ...) link.
    sym_uuids = {}
    part_page, comp_by_desig = _emit_canvas(
        top_pages, schem_el, part_insts, comp_by_name, pool, lib_name,
        proj_name, ns, sym_uuid_sink=sym_uuids)

    # Module instances: the sheet-pin position IS the port's connection
    # point on the parent (the IR wires already end there), so sheets carry
    # connectivity purely by geometry — same as component pins. Placement and
    # module-exists/rot validation already ran in the pre-pass above; reuse
    # the page it chose (modinst_page) so a point is resolved exactly once.
    for inst_el in module_insts:
        stem = inst_el.get('module')
        page = modinst_page[inst_el.get('name')]
        part_page[inst_el.get('name')] = page
        _emit_sheet(page, inst_el, mod_el_by_stem[stem],
                    mod_page_by_stem[stem], proj_name, ns,
                    modinst_pagenum[inst_el.get('name')])

    _emit_nets(top_pages, schem_el, part_page, comp_by_desig, pool, ns)

    # --- Net class assignment patterns (plan §5): exact strings, expanded
    # per module instance for module-local nets.
    class_patterns = []
    for net_el in schem_el.findall('net'):
        if net_el.get('class'):
            class_patterns.append({'netclass': net_el.get('class'),
                                   'pattern': f'/{net_el.get("name")}'})
    modinsts_by_stem = {}
    for inst_el in module_insts:
        modinsts_by_stem.setdefault(inst_el.get('module'), []).append(
            inst_el.get('name'))
    for mod_el in module_els:
        for net_el in mod_el.findall('net'):
            if not net_el.get('class'):
                continue
            for inst_name in modinsts_by_stem.get(mod_el.get('name'), []):
                class_patterns.append(
                    {'netclass': net_el.get('class'),
                     'pattern': f'/{inst_name}/{net_el.get("name")}'})

    # --- Write everything out.
    for page in top_pages:
        _write_page(page, out_dir, comp_by_name, ir_root, lib_name)
    for stem, mpage in mod_page_by_stem.items():
        _write_page(mpage, out_dir, comp_by_name, ir_root, lib_name,
                    is_module=True)
    # Hierarchy registry for .kicad_pro "sheets" (ground truth testData/t2):
    # every top-level page (by its instance uuid) then every module instance
    # (by its sheet uuid), each paired with a display name — this is how KiCad
    # 10 discovers all sheets of a flat multi-root design.
    sheet_registry = [(p.path_uuid, p.name) for p in top_pages]
    for inst_el in module_insts:
        sheet_registry.append(
            (_quuid(ns, 'sheet', inst_el.get('name')), inst_el.get('name')))
    _write_kicad_pro(out_dir, proj_name, ir_root, top_pages, class_patterns,
                     sheet_registry)

    # --- Board: KiCad's pcb editor only opens boards through a project,
    # so the layout (when present) is written as {proj}.kicad_pcb here.
    # sym_paths ties each footprint to its schematic symbol. Ground truth
    # testData/t2: a top-level part is `/{symbol_uuid}` (its page contributes
    # NO segment), a module-instance part is `/{module_sheet_uuid}/{symbol_uuid}`
    # — the sheet-instance uuid of that module occurrence + the (shared)
    # subsheet symbol uuid. Note the parent PAGE uuid is absent from the board
    # path even though the schematic symbol path carries it.
    if ir_root.find('layout') is not None:
        from babel.kicad_board_exporter import export_board_kicad
        sym_paths = {d: f'/{u}' for d, u in sym_uuids.items()}
        minst_page_name = {}
        for inst_el in module_insts:
            inst_name, stem = inst_el.get('name'), inst_el.get('module')
            sheet_uuid = _quuid(ns, 'sheet', inst_name)
            for canonical, symuuid in mod_sym_uuids.get(stem, {}).items():
                sym_paths[f'{inst_name}:{canonical}'] = f'/{sheet_uuid}/{symuuid}'
            # top-level page NAME this instance sits on — first segment of its
            # module-local net paths /{page}/{inst}/{localnet} (ground truth t2)
            minst_page_name[inst_name] = modinst_page[inst_name].name
        export_board_kicad(ir_path, out_dir / f'{proj_name}.kicad_pcb',
                           sym_paths=sym_paths, lib_nickname=lib_name,
                           minst_page_name=minst_page_name)

    pro_path = out_dir / f'{proj_name}.kicad_pro'
    print(f'Written: {pro_path}  ({len(top_pages)} page(s), '
          f'{len(module_els)} module(s), {len(part_insts)} placed part(s))')
    import_log.write(pro_path)
    return str(pro_path)


if __name__ == '__main__':
    import sys
    ir = sys.argv[1] if len(sys.argv) > 1 else 'outputs/multichannel.swprj'
    out = sys.argv[2] if len(sys.argv) > 2 else 'outputs/multichannel_kicad'
    export_project(ir, out)
