"""Altium .IntLib -> IR XML converter."""
import hashlib
import math
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

from altium_monkey import AltiumIntLib, AltiumSchLib, AltiumPcbLib
from babel.ir_util import sanitize_filename, clean_attr_name, arc_endpoints, place_layer
from babel.altium_exporter import _um_lw
from babel import import_log
from babel import altium_layers

_MILS_TO_UM = 25.4   # 1 mil = 25.4 µm


def _altium_overbar_to_eagle(text):
    """Altium's overbar notation -> IR/Eagle `!TEXT!` toggle (ir_schema.md
    "Надчёркивание"): a backslash AFTER a character overlines that ONE
    character (confirmed directly via altium_sch_svg_renderer.
    render_text_with_overline, not a guess) — `R\\E\\S\\E\\T\\` overlines
    "RESET" entirely, one backslash needed per character. Consecutive
    overlined characters are grouped into a single `!...!` run, same as
    _kicad_overbar_to_eagle's grouping (different source notation, same
    target)."""
    out = []
    overlined = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        has_bar = i + 1 < n and text[i + 1] == '\\'
        if has_bar and not overlined:
            out.append('!')
            overlined = True
        elif not has_bar and overlined:
            out.append('!')
            overlined = False
        out.append(ch)
        i += 2 if has_bar else 1
    if overlined:
        out.append('!')
    return ''.join(out)


def _pt_to_um(pt):
    """Altium font point size → IR text size, µm.

    Inverse of altium_exporter._font_id: Altium's point size does not follow
    the standard 72pt/inch typographic convention for rendered letter height —
    empirically, an 8pt font renders ~1.2mm tall, i.e. 1pt ~= 150 µm.
    """
    return round(float(pt) * 150)

_ELECTRICAL = {
    'Input':          'in',
    'Output':         'out',
    'Bidirectional':  'io',
    'Power':          'pwr',
    'Passive':        'pas',
    'Open Collector': 'out',
    'Open Emitter':   'out',
    'HiZ':            'out',
}
# Altium's own PinElectrical enum has no "Not Connected"/`nc` member at all
# (confirmed against altium_monkey's PIN_ELECTRICAL_NAMES — 8 values, none
# of them this) — unlike KiCad's `no_connect` and Eagle's `nc`, Altium
# genuinely has no electrical-type equivalent to import FROM. Falls through
# to the dict's own `.get(..., 'pas')` default below, same as any other
# unrecognized value — not a gap to plug, there's nothing on the Altium
# side to read.

_ORIENT_TO_ROT = {0: 0, 1: 90, 2: 180, 3: 270}  # for text

# Altium pin coord = body end; IR/Eagle pin coord = hot end (wire side).
# hot_end = body_end + length * direction(orient)
# IR rot = (altium_orient_deg + 180) % 360
_PIN_ORIENT_TO_ROT = {0: 180, 1: 270, 2: 0, 3: 90}

# altium_monkey TextJustification value → IR align string
_JUSTIFICATION = {
    0: 'bottom-left',
    1: 'bottom-center',
    2: 'bottom-right',
    3: 'center-left',
    4: 'center',
    5: 'center-right',
    6: 'top-left',
    7: 'top-center',
    8: 'top-right',
}

# Mirror vertical component of align (bottom↔top) for >VALUE placement
_FLIP_VERT = {
    'bottom-left':   'top-left',
    'bottom-center': 'top-center',
    'bottom-right':  'top-right',
    'center-left':   'center-left',
    'center':        'center',
    'center-right':  'center-right',
    'top-left':      'bottom-left',
    'top-center':    'bottom-center',
    'top-right':     'bottom-right',
}

# Altium PcbLayer ID → IR signed layer number as a string (ir_schema.md
# "Плата (Board IR)", canonical constants in ir_util.py).
# (numbering used by altium-monkey's PcbLayer enum)
# 1=Top, 32=Bottom, 33=TopOverlay, 34=BottomOverlay, 35=TopPaste, 36=BottomPaste,
# 57-72=Mechanical1-16, 74=Multi-Layer
# Mechanical 1..16 that resolve to no table row land here (+ layer index),
# the same "carry it as a plain user layer, don't guess" convention
# eagle_parser uses for unknown Eagle layers.
_USER_LAYER_BASE = 200
_unmapped_mech_seen = set()


def _log_unmapped_mech(fp_el, lid, kind):
    """One log line per (layer, kind) across a whole run — a library repeats
    the same undeclared mechanical layer on every one of its footprints."""
    key = (lid, kind)
    if key in _unmapped_mech_seen:
        return
    _unmapped_mech_seen.add(key)
    what = f'declared kind {kind}' if kind else 'no declared kind'
    import_log.log(f'Mechanical {lid - 56} ({what}) has no row in '
                    f'babel/data/altium_layers.tsv -> carried as user layer '
                    f'{_USER_LAYER_BASE + (lid - 57)}')


def _um(mils):
    """mils → integer µm string."""
    return str(round(float(mils) * _MILS_TO_UM))


def _f(v):
    """Format angle/ratio as compact float (degrees, percentages — not lengths)."""
    r = round(float(v), 4)
    return str(int(r)) if r == int(r) else str(r)


def _part_letter(n):
    """1-indexed Altium part id -> letter gate suffix (1->A, 2->B, ...).

    altium_monkey only exposes the numeric owner_part_id; the letter suffix
    (HL1A/HL1B) is purely an Altium UI convention applied when placing a
    multi-part symbol, not stored data — so this mapping is our own, chosen
    to match what an Altium user would actually see on the sheet.
    """
    return chr(ord('A') + n - 1)


def _iter_named_pins(sym, part_id=None):
    """Yield (ir_pin_name, pin) for sym's pins, filtered to one multi-part
    gate (part_id) or all of them (part_id=None), with the '@N' dedup-suffix
    numbering used everywhere an IR pin name is derived from a pin.

    Shared between symbol drawing and footprint pin-mapping so the two can't
    drift apart and disagree on a pin's name.
    """
    def _keep(pin):
        if part_id is None:
            return True
        opid = getattr(pin, 'owner_part_id', None)
        return opid is None or opid in (-1, 0) or opid == part_id

    seen: dict[str, int] = {}
    for pin in sym.pins:
        if not _keep(pin):
            continue
        pname = clean_attr_name(pin.name or str(pin.designator))
        seen[pname] = seen.get(pname, 0) + 1
        n = seen[pname]
        yield (pname if n == 1 else f'{pname}@{n}', pin)


def _id_xform(x, y):
    return x, y


def emit_rect_geometry(sym_el, rect, xform=_id_xform):
    """Shared with altium_project_parser.py (placed-instance path) — see
    that module's docstring for why the two importers must agree on this
    geometry math rather than keep two independent implementations."""
    x1, y1 = xform(rect.location_mils.x_mils, rect.location_mils.y_mils)
    x2, y2 = xform(rect.corner_mils.x_mils, rect.corner_mils.y_mils)
    x1, y1, x2, y2 = _um(x1), _um(y1), _um(x2), _um(y2)
    w = str(_um_lw(rect.line_width))
    for ax1, ay1, ax2, ay2 in [(x1,y1,x2,y1),(x2,y1,x2,y2),(x2,y2,x1,y2),(x1,y2,x1,y1)]:
        ET.SubElement(sym_el, 'line', x1=ax1, y1=ay1, x2=ax2, y2=ay2, width=w)


def emit_polyline_geometry(sym_el, pl, xform=_id_xform):
    pts = [xform(p.x_mils, p.y_mils) for p in pl.points_mils]
    w = str(_um_lw(pl.line_width))
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        ET.SubElement(sym_el, 'line',
                      x1=_um(ax), y1=_um(ay), x2=_um(bx), y2=_um(by), width=w)


def emit_line_geometry(sym_el, ln, xform=_id_xform):
    x1, y1 = xform(ln.location_mils.x_mils, ln.location_mils.y_mils)
    x2, y2 = xform(ln.corner_mils.x_mils, ln.corner_mils.y_mils)
    ET.SubElement(sym_el, 'line',
                  x1=_um(x1), y1=_um(y1), x2=_um(x2), y2=_um(y2),
                  width=str(_um_lw(ln.line_width)))


def emit_arc_geometry(sym_el, arc, xform=_id_xform):
    """AltiumSchEllipticalArc is a subclass of AltiumSchArc and adds a
    second, minor-axis radius. IR has no ellipse-arc primitive, so collapse
    to a circular arc using the smaller of the two radii — stays inside the
    original ellipse. Altium's native arc form is the CENTER one — the
    endpoint-canon conversion (ir_util's arc-math block) happens here, at
    the Altium boundary; full circles are <shape roundness=100>, not arcs."""
    lx, ly = xform(arc.location_mils.x_mils, arc.location_mils.y_mils)
    radius_mils = arc.radius_mils
    secondary_mils = getattr(arc, 'secondary_radius_mils', None)
    if secondary_mils is not None:
        radius_mils = min(radius_mils, secondary_mils)
    sweep = (arc.end_angle - arc.start_angle) % 360 or 360
    if sweep >= 360:
        ET.SubElement(sym_el, 'shape',
                      x=_um(lx), y=_um(ly),
                      w=_um(radius_mils * 2), h=_um(radius_mils * 2),
                      roundness='100', rot='0',
                      outline=str(_um_lw(arc.line_width)))
    else:
        x1, y1, x2, y2, curve = arc_endpoints(
            float(lx), float(ly), float(radius_mils),
            float(arc.start_angle), sweep)
        ET.SubElement(sym_el, 'arc',
                      x1=_um(x1), y1=_um(y1), x2=_um(x2), y2=_um(y2),
                      curve=_f(curve),
                      width=str(_um_lw(arc.line_width)))


def emit_ellipse_geometry(sym_el, ell, xform=_id_xform):
    """Same min-radius collapse as elliptical arcs, as a full circle."""
    lx, ly = xform(ell.location_mils.x_mils, ell.location_mils.y_mils)
    radius_mils = min(ell.radius_mils, ell.secondary_radius_mils)
    ET.SubElement(sym_el, 'shape',
                  x=_um(lx), y=_um(ly),
                  w=_um(radius_mils * 2), h=_um(radius_mils * 2),
                  roundness='100', rot='0',
                  outline=str(_um_lw(ell.line_width)))


def emit_polygon_geometry(sym_el, pol, xform=_id_xform):
    """Line-loop, NOT IR <polygon fill=...> — established convention for
    the schematic-symbol domain (fill has no defined rendering meaning
    there); do not diverge per-importer, see module docstring for why."""
    pts = [xform(p.x_mils, p.y_mils) for p in pol.points_mils]
    w = str(_um_lw(pol.line_width))
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        ET.SubElement(sym_el, 'line',
                      x1=_um(ax), y1=_um(ay), x2=_um(bx), y2=_um(by), width=w)


def emit_label_geometry(sym_el, lbl, xform=_id_xform, layer='SYMBOLS'):
    """Free text (AltiumSchLabel) -> IR <text>. Returns the element, or None
    for a hidden/empty label. `layer` differs by domain — SYMBOLS for symbol
    body text, GRAPHIC for a sheet-level annotation (altium_project_parser's
    decorative-canvas path) — the geometry math itself is the same and must
    not be duplicated, same rule as the shape emitters above."""
    if lbl.is_hidden or not lbl.text:
        return None
    lx, ly = xform(lbl.location.x_mils, lbl.location.y_mils)
    lalign = _JUSTIFICATION.get(lbl.justification.value if hasattr(lbl.justification, 'value') else 0, 'bottom-left')
    lrot   = _ORIENT_TO_ROT.get(lbl.orientation.value if hasattr(lbl.orientation, 'value') else 0, 0)
    lsz    = str(_pt_to_um(lbl.font.size)) if lbl.font else '1270'
    el = ET.SubElement(sym_el, 'text',
                       x=_um(lx), y=_um(ly), size=lsz, rot=str(lrot),
                       align=lalign, layer=layer)
    el.text = lbl.text
    return el


def _convert_symbol(sym, sym_name, part_id=None):
    """Build a pool-ready <symbol name=...> from an altium-monkey symbol.

    part_id, if given, restricts graphics to one part of a multi-part Altium
    symbol: objects owned by that part (owner_part_id == part_id) plus objects
    shared across all parts (owner_part_id in (None, -1, 0) — e.g. the
    >NAME/>Value placeholders) are kept; another part's objects are dropped.
    With part_id=None (single-part symbols) everything is kept, matching the
    old behaviour.
    """
    def _keep(obj):
        if part_id is None:
            return True
        opid = getattr(obj, 'owner_part_id', None)
        return opid is None or opid in (-1, 0) or opid == part_id

    sym_el = ET.Element('symbol', name=sym_name)

    for rect in filter(_keep, sym.rectangles):
        emit_rect_geometry(sym_el, rect)
    for pl in filter(_keep, sym.polylines):
        emit_polyline_geometry(sym_el, pl)
    for ln in filter(_keep, sym.lines):
        emit_line_geometry(sym_el, ln)
    for arc in filter(_keep, sym.arcs):
        emit_arc_geometry(sym_el, arc)
    for ell in filter(_keep, sym.ellipses):
        emit_ellipse_geometry(sym_el, ell)
    for pol in filter(_keep, sym.polygons):
        emit_polygon_geometry(sym_el, pol)

    for ir_pin_name, pin in _iter_named_pins(sym, part_id):
        direction = _ELECTRICAL.get(pin.electrical_name, 'pas')
        orient    = pin.orientation  # 0-3
        rot       = _PIN_ORIENT_TO_ROT.get(orient, 180)
        # Altium x,y = body end; shift to hot end (wire connection point)
        orient_deg = orient * 90
        length_um  = float(pin.length_mils) * _MILS_TO_UM
        px_um = float(pin.x_mils) * _MILS_TO_UM + length_um * math.cos(math.radians(orient_deg))
        py_um = float(pin.y_mils) * _MILS_TO_UM + length_um * math.sin(math.radians(orient_deg))
        ET.SubElement(sym_el, 'pin',
                      name=ir_pin_name,
                      x=str(round(px_um)),
                      y=str(round(py_um)),
                      rot=str(rot),
                      length=str(round(length_um)),
                      direction=direction,
                      pinvis='1' if pin.show_name else '0',
                      padvis='1' if pin.show_designator else '0')

    for lbl in filter(_keep, sym.labels):
        emit_label_geometry(sym_el, lbl)

    # >NAME from designator
    desgns = list(filter(_keep, sym.designators))
    if desgns:
        d     = desgns[0]
        nx    = _um(d.location.x_mils)
        ny    = _um(d.location.y_mils)
        align = _JUSTIFICATION.get(d.justification.value, 'bottom-left')
        rot   = _ORIENT_TO_ROT.get(d.orientation.value if hasattr(d.orientation, 'value') else 0, 0)
        sz    = str(_pt_to_um(d.font.size)) if d.font else '1270'
    else:
        nx, ny, align, rot, sz = '2540', '2540', 'bottom-left', 0, '1270'
    ET.SubElement(sym_el, 'text',
                  x=nx, y=ny, size=sz, rot=str(rot),
                  align=align, layer='NAMES').text = '>NAME'

    # Visible parameters → >CleanedName placeholder texts
    for par in filter(_keep, sym.parameters):
        if par.is_hidden:
            continue
        pname = clean_attr_name(par.name)
        if not pname or pname.lower() == 'designator':
            continue
        px     = _um(par.location.x_mils)
        py     = _um(par.location.y_mils)
        palign = _JUSTIFICATION.get(par.justification.value if hasattr(par.justification, 'value') else 0, 'bottom-left')
        prot   = _ORIENT_TO_ROT.get(par.orientation.value if hasattr(par.orientation, 'value') else 0, 0)
        psz    = str(_pt_to_um(par.font.size)) if par.font else '1270'
        # Altium has two parameters that both mean "device value" — Comment
        # (the always-present schematic-visible field) and the ordinary,
        # optional Value parameter some libraries place directly instead.
        # Map either onto the canonical >VALUE placeholder, same field
        # Eagle/KiCad use (see altium_exporter.py's reverse mapping).
        placeholder = 'VALUE' if pname.lower() in ('value', 'comment') else pname
        ET.SubElement(sym_el, 'text',
                      x=px, y=py, size=psz, rot=str(prot),
                      align=palign, layer='VALUES').text = f'>{placeholder}'

    return sym_el


def _symbol_geometry_hash(sym_el):
    """Hash a symbol's children (line/arc/pin/text/...), name-independent.

    Same IntLib symbol record duplicated under different names (e.g. one per
    DesignItemId/Part Number) serializes its geometry in the same child order,
    since it comes from the same parser code path — so plain document-order
    serialization is enough to detect a duplicate without sorting.
    """
    parts = []
    for child in sym_el:
        attrs = ' '.join(f'{k}={v}' for k, v in child.attrib.items())
        parts.append(f'<{child.tag} {attrs}>{child.text or ""}')
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()


# PcbTextJustification value → IR align
_PCB_JUST = {
    0: 'bottom-left',   # MANUAL
    1: 'top-left',      # LEFT_TOP
    2: 'center-left',   # LEFT_CENTER
    3: 'bottom-left',   # LEFT_BOTTOM
    4: 'top-center',    # CENTER_TOP
    5: 'center',        # CENTER_CENTER
    6: 'bottom-center', # CENTER_BOTTOM
    7: 'top-right',     # RIGHT_TOP
    8: 'center-right',  # RIGHT_CENTER
    9: 'bottom-right',  # RIGHT_BOTTOM
}

_INTERNAL_PER_MIL = 10000.0   # altium-monkey internal units per mil (for pad height)
_INTERNAL_TO_UM   = 25.4 / 10000.0   # internal units → µm (0.00254 µm/unit)


def _mech_layer_kinds(owner):
    """{Altium layer id -> MechanicalLayerKind NAME} declared by a PcbLib or
    PcbDoc, or {} when it declares none. What each Mechanical 1..16 layer
    MEANS is per-project data Altium stores in the file — read it, never
    infer it from the layer number (see babel/altium_layers.py)."""
    raw = getattr(owner, 'mechanical_layer_kinds', None) or {}
    out = {}
    for lid, kind in raw.items():
        try:
            lid = int(lid)
        except (TypeError, ValueError):
            continue
        if 57 <= lid <= 72:
            out[lid] = getattr(kind, 'name', str(kind))
    return out


def _convert_footprint(fp, fp_el, xform=_id_xform, bottom=False, rot_offset=0.0,
                        mech_kinds=None):
    """Append footprint geometry to fp_el from an object exposing .pads/
    .tracks/.arcs/.texts — an altium-monkey AltiumPcbFootprint (library,
    xform=identity, bottom=False: pads/tracks/etc. are already footprint-
    local by construction) OR the same-shaped collections filtered off a
    PLACED AltiumPcbDoc component (project import — same record classes,
    see altium_project_parser.py, which supplies xform=inverse-placement
    and bottom=<is this instance mounted on the bottom side>). One
    implementation, two callers — do not fork a second footprint converter.

    Geometry goes in as DIRECT children of <footprint> with a layer="N"
    attribute (unified board/footprint layer model, ir_schema.md "Плата
    (Board IR)"); <smd>/<pad> carry no layer (copper by construction,
    far-side smd gets an explicit layer="-1").

    Layer projection is the user-editable table in babel/data (see
    babel/altium_layers.py): FIXED layers resolve by their stable id, while
    a MECHANICAL layer (57..72) resolves through the KIND its own source
    file declares for it (`mech_kinds`, from _mech_layer_kinds). The number
    itself means nothing — Assembly sits on Mechanical 2 in one design and
    Mechanical 13 in another. An undeclared mechanical layer, or a kind the
    table doesn't carry, becomes a plain user layer and is logged, rather
    than being guessed onto a semantic one.

    The table encodes each layer as if the owning object were on the TOP
    side (IR's own fixed sign convention). For a library footprint that's
    already correct (no such thing as "placed bottom" in a library). For a
    PLACED instance, `pad.layer`/`track.layer`/etc. carry the objects' true
    ABSOLUTE side — `ir_util.place_layer(ln, bottom)` (same sign-toggle used
    board-wide for placement) converts that absolute reading into the
    footprint-LOCAL, mount-side-relative sign IR wants; it's a no-op when
    bottom=False, so the library call path is unaffected.
    """
    mech_kinds = mech_kinds or {}

    def _layer_n(lyr_id):
        lid = int(lyr_id)
        if 57 <= lid <= 72:
            kind = mech_kinds.get(lid)
            ln = altium_layers.altium_to_ir(kind) if kind else None
            if ln is None:
                ln = _USER_LAYER_BASE + (lid - 57)
                _log_unmapped_mech(fp_el, lid, kind)
            ln = str(ln)
        else:
            name = altium_layers.FIXED_LAYER_NAME.get(lid)
            ln = altium_layers.altium_to_ir(name) if name else None
            if ln is None:
                return None
            ln = str(ln)
        return place_layer(ln, bottom)

    # Non-electrical pads (fiducials, mounting holes) carry an empty Altium
    # designator. Eagle requires a non-empty smd/pad name, so number them,
    # skipping any value already used by a real designator.
    used_names = {str(p.designator).strip() for p in fp.pads if str(p.designator).strip()}
    anon_n = 0

    # IR requires a unique name per pad, and an SMD pad lives on exactly one
    # layer (ir_schema.md) — but Altium genuinely stores two independent pad
    # records under the SAME designator for a part that has copper on both
    # Top and Bottom under one logical pin (ground-truth: 8AO-VI's "ME BUS FE
    # CONTACT" connector, designator "1" on layer Top and layer Bottom —
    # confirmed by the user looking at the real footprint in Altium: "два
    # физически разных пада, но у них совпадает дезигнатор" — the shared
    # designator is how Altium associates both with the one schematic pin).
    # Same disambiguation convention as kicad_parser.py's own duplicate-pad-
    # number handling (raw name, then raw+'a', raw+'b', ...) — pad_name_groups
    # maps the original Altium designator to every synthesized IR name sharing
    # it, so the caller can map one schematic pin to all of them.
    pad_name_groups = {}

    for pad in fp.pads:
        ln = _layer_n(pad.layer)
        if ln not in ('1', '-1'):
            continue
        px, py = xform(pad.x_mils, pad.y_mils)
        x    = _um(px)
        y    = _um(py)
        w    = _um(pad.width_mils)
        h    = str(round(float(pad.height) * _INTERNAL_TO_UM))
        raw_name = str(pad.designator).strip()
        if raw_name:
            dup_n = len(pad_name_groups.get(raw_name, []))
            name = raw_name if dup_n == 0 else f'{raw_name}{chr(ord("a") + dup_n - 1)}'
            pad_name_groups.setdefault(raw_name, []).append(name)
            if dup_n:
                import_log.log(f'{fp_el.get("name")}: pad "{raw_name}" appears '
                                f'{dup_n + 1} times (same designator, different '
                                f'layers) -> disambiguated as "{name}"')
        else:
            anon_n += 1
            while str(anon_n) in used_names:
                anon_n += 1
            name = str(anon_n)
            used_names.add(name)
            import_log.log(f'{fp_el.get("name")}: blank pad designator '
                            f'(fiducial/mounting hole) -> synthesized "{name}"')
        shape_id = int(pad.effective_top_shape)

        if not pad.is_smt:
            el = ET.SubElement(fp_el, 'pad')
            el.set('name', name)
            el.set('x', x); el.set('y', y)
            el.set('drill', _um(pad.hole_size_mils))
            diam_mils = min(float(pad.width_mils), float(pad.height) / _INTERNAL_PER_MIL)
            el.set('diameter', _um(diam_mils))
            el.set('shape', 'square' if shape_id == 2 else 'round')
        else:
            if shape_id == 1:    # CIRCLE
                roundness = '100'
            elif shape_id == 4:  # ROUNDED_RECTANGLE
                crp = pad.corner_radius_percentage
                roundness = str(int(crp)) if crp else '50'
            elif shape_id == 3:  # OCTAGONAL
                roundness = '50'
            else:                # RECTANGLE, CUSTOM, unknown
                roundness = '0'
            el = ET.SubElement(fp_el, 'smd')
            if ln == '-1':
                el.set('layer', '-1')
            el.set('name', name)
            el.set('x', x); el.set('y', y)
            el.set('width', w); el.set('height', h)
            el.set('roundness', roundness)
            rot = (float(pad.rotation or 0) - rot_offset) % 360
            if rot:
                el.set('rot', _f(rot))

    for track in fp.tracks:
        ln = _layer_n(track.layer)
        if ln is None:
            continue
        x1, y1 = xform(track.start_x_mils, track.start_y_mils)
        x2, y2 = xform(track.end_x_mils, track.end_y_mils)
        el = ET.SubElement(fp_el, 'line')
        el.set('x1', _um(x1)); el.set('y1', _um(y1))
        el.set('x2', _um(x2)); el.set('y2', _um(y2))
        el.set('width', _um(track.width_mils))
        el.set('layer', ln)

    for arc in fp.arcs:
        ln = _layer_n(arc.layer)
        if ln is None:
            continue
        cx, cy = xform(arc.center_x_mils, arc.center_y_mils)
        start = float(arc.start_angle) - rot_offset
        end   = float(arc.end_angle) - rot_offset
        sweep = (end - start) % 360 or 360
        if sweep >= 360:
            el = ET.SubElement(fp_el, 'shape')
            el.set('layer', ln)
            el.set('x', _um(cx)); el.set('y', _um(cy))
            el.set('w', _um(float(arc.radius_mils) * 2))
            el.set('h', _um(float(arc.radius_mils) * 2))
            el.set('roundness', '100'); el.set('rot', '0')
            el.set('outline', _um(arc.width_mils))
        else:
            x1, y1, x2, y2, curve = arc_endpoints(
                float(cx), float(cy),
                float(arc.radius_mils), start % 360, sweep)
            el = ET.SubElement(fp_el, 'arc')
            el.set('layer', ln)
            el.set('x1', _um(x1)); el.set('y1', _um(y1))
            el.set('x2', _um(x2)); el.set('y2', _um(y2))
            el.set('curve', _f(curve))
            el.set('width', _um(arc.width_mils))

    for txt in fp.texts:
        ln = _layer_n(txt.layer)
        if ln is None:
            continue
        content = txt.text_content or ''
        # Library footprint text stores the literal template token
        # ('.Designator'/'.Comment'); a PLACED PcbDoc text object instead
        # carries the string ALREADY resolved to that instance's real
        # designator/comment ("X1"/"MEPLC40MT-PCB-6") — using the string
        # check alone would bake one instance's literal designator into the
        # shared pool footprint. `is_designator`/`is_comment` are reliable
        # in both cases (confirmed empirically on a placed IND80S28 board
        # text pair: is_designator=True/'X1' and is_comment=True/
        # 'MEPLC40MT-PCB-6' — the library string form was never actually
        # exercised by real test data, so it stays only as a fallback).
        # A special string embedded in a longer PCB string is wrapped in
        # apostrophes (Altium's concatenation syntax: "Rev '.Comment'");
        # a string that IS just one special string may come either bare or
        # quoted ("'.Designator'" — degenerate one-element concatenation),
        # so strip a matching apostrophe pair before comparing.
        bare = content[1:-1] if len(content) > 2 and content[0] == "'" and content[-1] == "'" else content
        if getattr(txt, 'is_designator', False) or bare == '.Designator':
            content = '>NAME'
        elif getattr(txt, 'is_comment', False) or bare == '.Comment':
            content = '>VALUE'
        elif re.search(r"'\.[A-Za-z_][A-Za-z0-9_]*'", content):
            # Mixed concatenation or some other quoted special string —
            # not resolved, kept literal; surface it instead of hiding it.
            import_log.log(f'{fp_el.get("name")}: unresolved Altium special '
                           f'string in footprint text {content!r} — kept as '
                           f'literal text')
        j     = txt.effective_justification
        jval  = j.value if hasattr(j, 'value') else int(j)
        align = _PCB_JUST.get(jval, 'bottom-left')
        tx, ty = xform(txt.x_mils, txt.y_mils)
        el = ET.SubElement(fp_el, 'text')
        el.set('layer', ln)
        el.set('x', _um(tx)); el.set('y', _um(ty))
        el.set('size', _um(txt.height_mils) if txt.height_mils else '1000')
        el.set('rot', _f((float(txt.rotation or 0) - rot_offset) % 360))
        el.set('align', align)
        el.text = content

    return pad_name_groups



def _do_model_extraction(orig_to_compel, intlib, pcblib_cache, models_dir):
    """Extract embedded STEP models, copy to models_dir/{fp_name}.step, add <model3d> to IR."""
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    # Extract all models from every parsed PCBLib into a temp dir
    pcblib_model_map = {}   # mvp → {original_name_lower: temp Path}
    with tempfile.TemporaryDirectory() as tmp_extract:
        for mvp, pcblib in pcblib_cache['parsed'].items():
            sub = Path(tmp_extract) / re.sub(r'[^\w]', '_', mvp)
            try:
                entries   = pcblib.get_embedded_model_entries()
                extracted = pcblib.extract_embedded_models(sub)
            except Exception:
                continue
            # Map by GUID so duplicate model names (step_temp.STEP etc.) resolve correctly
            id_map = {}
            for (model, _), path in zip(entries, extracted):
                id_map[model.id] = path
            pcblib_model_map[mvp] = id_map

        for comp in intlib.components:
            comp_el = orig_to_compel.get(comp.name)
            if comp_el is None:
                continue

            for model in comp.models:
                if model.model_type != 'PCBLIB':
                    continue
                mvp = model.virtual_path.lstrip(':\\').replace('\\', '/')
                pcblib = pcblib_cache['parsed'].get(mvp)
                if pcblib is None:
                    continue

                fp = pcblib.find_footprint(model.name)
                if fp is None:
                    continue

                fp_el = comp_el.find(f'footprint[@name="{model.name}"]')
                if fp_el is None:
                    continue

                # A merged generic component (e.g. "R") can hold one footprint
                # shared by several original IntLib rows (R0603/R0603_1) — only
                # extract/attach the model once.
                if fp_el.find('model3d') is not None:
                    continue

                body = next((b for b in fp.component_bodies if b.model_is_embedded), None)
                if body is None:
                    continue

                src_path = pcblib_model_map.get(mvp, {}).get(body.model_id)
                if src_path is None or not src_path.exists():
                    continue

                step_file = f'{sanitize_filename(model.name)}.step'
                shutil.copy2(str(src_path), str(models_dir / step_file))

                m3 = ET.Element('model3d')
                m3.set('tx', str(round(body.model_2d_x  * _INTERNAL_TO_UM)))
                m3.set('ty', str(round(body.model_2d_y  * _INTERNAL_TO_UM)))
                m3.set('tz', str(round(body.model_3d_dz * _INTERNAL_TO_UM)))
                m3.set('rx', _f(body.model_3d_rotx % 360))
                m3.set('ry', _f(body.model_3d_roty % 360))
                m3.set('rz', _f(body.model_3d_rotz % 360))
                fp_el.insert(0, m3)


_PARAM_REF = re.compile(r'^=([A-Za-z_][A-Za-z0-9_ ]*)$')


def _designator_prefix(sym):
    """Strip the placeholder '?' from the symbol's designator pattern (R? -> R)."""
    desgns = list(sym.designators)
    if not desgns or not desgns[0].text:
        return ''
    return desgns[0].text.rstrip('?') or desgns[0].text


def _is_param_alias(par, all_params):
    """True if par's text is a '=OtherParamName' reference to a real sibling
    parameter — Altium's way of mirroring one field onto another (e.g. a
    Comment field that just displays Value). The referenced parameter already
    carries the data under its own name, so the alias would only duplicate it.
    """
    m = _PARAM_REF.match((par.text or '').strip())
    if not m:
        return False
    ref_name = m.group(1).strip()
    return any(p.name == ref_name for p in all_params)


# Parameter names that mark a component as a VALUE-PARAMETRIZED catalog row
# of a generic device family (one IntLib row per catalog value, all sharing a
# symbol) — the generic-merge trigger in _convert_component. A hand-drawn
# SchLib part uses the literal 'Value' parameter; catalog/Vault libraries
# (ground truth BC2087, Luxonis) instead name the parameter after the
# physical quantity: Resistance (R rows), Capacitance (C), Inductance (L),
# Impedance (ferrite beads; a bead row can carry Inductance AND Impedance).
# The original Value-only trigger left 28 CRCW/RC0402 resistor rows and a
# dozen GRM/CL capacitor rows as separate one-value "devices" in the
# converted library while their shared-symbol dedup had already proven them
# one family (caught by the user reading BC2087.lbr).
_GENERIC_VALUE_PARAMS = ('Value', 'Resistance', 'Capacitance', 'Inductance',
                         'Impedance')


def _register_symbol(pool, symbols_el, geom_pool, sym, name, part_id=None):
    """Build (if needed) and register one symbol — or one part of a
    multi-part symbol — in the library pool. Dedups by exact name first
    (cheap, no rebuild needed); then by geometry hash, since IntLib gives
    one symbol record per DesignItemId even when the Library Ref (and thus
    the geometry) is shared — e.g. R0603/R0603_1 differing only in Value.
    Returns the registered <symbol> Element (the canonical one on a hash hit).
    """
    if name not in pool:
        sym_el = _convert_symbol(sym, name, part_id=part_id)
        ghash = _symbol_geometry_hash(sym_el)
        if ghash in geom_pool:
            pool[name] = pool[geom_pool[ghash]]
        else:
            # The ELEMENT name must stay unique in <symbols> even though the
            # pool key (this catalog row's name) is already unique: the
            # generic-merge block renames a family's pooled symbol to the
            # bare PREFIX ('R'), and a later, geometrically different row
            # whose own catalog name is literally that prefix would
            # otherwise register a second <symbol name="R"> — two same-named
            # symbols make every by-name lookup downstream silently pick
            # one (the exact failure mode of the 8AO-VI <component name="C">
            # collision, one level down).
            el_name, n = name, 1
            while symbols_el.find(f'symbol[@name="{el_name}"]') is not None:
                el_name = f'{name}@{n}'
                n += 1
            sym_el.set('name', el_name)
            geom_pool[ghash] = name
            pool[name] = sym_el
            symbols_el.append(sym_el)
    return pool[name]


def _explicit_pin_map(sym, fp_name):
    """{pin designator -> [raw pad designators]} from the symbol's own
    MAP_DEFINER records for one footprint implementation, or {} if it
    declares none.

    Altium stores an explicit pin<->pad map per (symbol, footprint) pairing
    as AltiumSchMapDefiner children of the implementation — the same records
    altium_exporter.py already WRITES (`designator_interface` = schematic pin
    designator, `implementation_designators` = pad name list). It is a
    PARTIAL override: only pins whose mapping isn't plain designator-equals-
    pad-name are listed, everything else falls through to name matching.
    Ground truth (RoXY_Motherboard, AMS1117 -> SOT89): the symbol has 4 pins
    (1/2/3/4, two of them named VOUT) against a 3-pad package, and a single
    MapDefiner '4' -> ['2'] carries the whole story; without reading it, pin
    4 found no pad "4" and was dropped with a warning.
    A pad list of ['null'] (or an empty designator_interface) is Altium's
    "connects to nothing" marker — recorded as an explicit EMPTY list so the
    caller suppresses the pin instead of falling back to name matching.
    Rare but real: 3 implementations across the three test projects, 0 in
    IND80S28/8AO-VI — hence [[feedback_explicit_pin_mapping]]: read the
    declared map, never infer it.
    """
    out = {}
    for imp in getattr(sym, 'implementations', ()):
        if getattr(imp, 'model_type', None) != 'PCBLIB':
            continue
        if getattr(imp, 'model_name', None) != fp_name:
            continue
        for k in getattr(imp, 'children', ()):
            if type(k).__name__ != 'AltiumSchMapDefiner':
                continue
            pin_des = (getattr(k, 'designator_interface', '') or '').strip()
            if not pin_des:
                continue
            pads = [str(p).strip() for p in (getattr(k, 'implementation_designators', None) or [])]
            pads = [p for p in pads if p and p.lower() != 'null']
            out[pin_des] = pads
    return out


def _find_pcb_footprint(pcblib_cache, name, virtual_path=None):
    """Locate a footprint by NAME in the library's PcbLib stream(s).

    `virtual_path` pins the lookup to one stream (the library path knows it
    from the model record); without it every packaged PcbLib stream is
    searched in order — the project path only has the bare footprint name
    the schematic instance carries, no stream reference.
    Exact match first, then case-insensitive: same storage-name-vs-display-
    name drift as the SchLib '/'->'_' substitution, just case instead of a
    character (ground truth on 8AO-VI: "RFID 13.56 MHz" declares PCB model
    'RFID_15mm' while the compiled PcbLib stores it as 'RFID_15MM').
    """
    vps = [virtual_path] if virtual_path else list(pcblib_cache['paths'])
    for vp in vps:
        pcb_file = pcblib_cache['paths'].get(vp)
        if pcb_file is None:
            continue
        if vp not in pcblib_cache['parsed']:
            try:
                pcblib_cache['parsed'][vp] = AltiumPcbLib.from_file(str(pcb_file))
            except Exception:
                continue
        pcblib = pcblib_cache['parsed'][vp]
        fp = pcblib.find_footprint(name)
        if fp is None:
            fp = next((f for f in pcblib.footprints
                        if f.name.lower() == name.lower()), None)
        if fp is not None:
            # The OWNING pcblib comes back too: mechanical-layer meanings are
            # declared per file, so the footprint alone can't be projected.
            return fp, pcblib
    return None, None


def _convert_component(comp, schlib_cache, pcblib_cache, pool, symbols_el, geom_pool,
                       footprint_names=None):
    """Convert one IntLibComponent → component info dict, or None on error.

    Returns {'orig_name', 'prefix', 'is_generic', 'is_multi_gate', 'symbol_name',
    'symbol_el', 'gates', 'description', 'footprints': [(name, <footprint> Element), ...],
    'attrs': [(name, value), ...]}. ('symbol_name'/'symbol_el' are None for a
    multi-gate component; 'gates' is None for a single-symbol one.)
    Building plain Elements here (instead of attaching them to the tree) lets
    the caller merge generic value-parametrized parts (R/C/...) that IntLib
    splits into one row per catalog value, before deciding the final tree shape.
    """
    vp = comp.virtual_path.lstrip(':\\').replace('\\', '/')
    sch_file = schlib_cache['paths'].get(vp)
    if sch_file is None:
        return None

    if vp not in schlib_cache['parsed']:
        try:
            schlib_cache['parsed'][vp] = AltiumSchLib(str(sch_file))
        except Exception:
            return None
    schlib = schlib_cache['parsed'][vp]

    # OLE compound-file storage/stream names can't contain '/', so Altium
    # substitutes '_' in the symbol's own stored name while the IntLib
    # component list keeps the original '/' (e.g. "M24LR04E-RMN6T/2" vs
    # storage name "M24LR04E-RMN6T_2") — not a naming clash with some other
    # part, ground-truth verified on 8AO-VI's IntLib.
    sym = next((s for s in schlib.symbols if s.name == comp.name), None)
    if sym is None and '/' in comp.name:
        sym = next((s for s in schlib.symbols if s.name == comp.name.replace('/', '_')), None)
    if sym is None:
        return None

    # A multi-part Altium symbol (Part Count > 1, e.g. a 2-diode LED package)
    # draws every part's geometry into the SAME symbol record, distinguished
    # only by owner_part_id — converting it as one IR symbol would overlay
    # all parts' geometry on top of each other. Split it into one IR symbol
    # per part instead, wired up as an IR multi-gate component.
    is_multi_gate = sym.part_count > 1
    if is_multi_gate:
        gates = []
        for part_idx in range(1, sym.part_count + 1):
            gate_name = _part_letter(part_idx)
            part_sym_el = _register_symbol(pool, symbols_el, geom_pool, sym,
                                            f'{sym.name}_{gate_name}', part_id=part_idx)
            gates.append((gate_name, part_sym_el.get('name')))
        canonical_sym_name, sym_el_for_info = None, None
    else:
        sym_el = _register_symbol(pool, symbols_el, geom_pool, sym, sym.name)
        canonical_sym_name, sym_el_for_info, gates = sym_el.get('name'), sym_el, None

    all_params = list(sym.parameters)
    # Generic value-parametrized merging (R/C/...) doesn't make sense for a
    # multi-gate part (an LED pair isn't "the same device at a different
    # catalog value"), so it's never treated as a merge candidate.
    is_generic = (not is_multi_gate) and any(p.name in _GENERIC_VALUE_PARAMS
                                              for p in all_params)

    # Which footprints belong to this component.
    #
    # `footprint_names` (project import) = the names the SCHEMATIC actually
    # associates with this device. Altium has no real library-level "device"
    # concept (same as KiCad — the symbol<->footprint association lives on
    # the schematic instance, not in the library), so on the project path
    # the association is read from the schematic and only the SYMBOLS and
    # FOOTPRINTS themselves come out of the IntLib. Per the user:
    # "при импорте альтиум проекта мы должны брать СИМВОЛЫ и футпринты из
    # интлиб, а вот их ассоциацию смотреть непосредственно в схеме".
    # Ground truth for why the library's own list can't be trusted:
    # RoXY_Motherboard's SN74LVC2G14DCKRE4 has an EMPTY IntLib model list
    # while its SchLib symbol implementation and both placed instances
    # (U2/U4) name a real, packaged footprint 'SOT65P210X110-6N'.
    #
    # `footprint_names is None` (pure library import, convert()) keeps the
    # library's own model list — there's no schematic to ask.
    if footprint_names is None:
        wanted = [(m.name, m.virtual_path.lstrip(':\\').replace('\\', '/'))
                  for m in comp.models if m.model_type == 'PCBLIB']
    else:
        wanted = [(n, None) for n in sorted(footprint_names)]

    footprints = []
    for fp_name, mvp in wanted:
        fp, owning_pcblib = _find_pcb_footprint(pcblib_cache, fp_name, mvp)
        if fp is None:
            continue

        fp_el = ET.Element('footprint', name=fp_name)
        pad_name_groups = _convert_footprint(
            fp, fp_el, mech_kinds=_mech_layer_kinds(owning_pcblib))

        # Pin -> pad mapping by matching designators. pad_name_groups (from
        # _convert_footprint) maps the raw Altium pad designator to every
        # disambiguated IR pad name sharing it — usually one, but see the
        # "ME BUS FE CONTACT" case above for a designator shared by two
        # physical pads (one per layer): all of them go on the SAME <map>,
        # space-separated, exactly the multi-pad-per-pin convention
        # kicad_parser.py's own _pin_mapping already uses and
        # altium_project_parser.py's pad_to_pin builder already consumes
        # (`for pad in m.get('pad', '').split(): ...`).
        # Explicit MAP_DEFINER first (the declared truth), implicit
        # designator==pad-name matching only for pins it doesn't cover.
        explicit = _explicit_pin_map(sym, fp_name)

        def _pads_for(pin):
            des = str(pin.designator)
            if des in explicit:
                # Declared mapping — translate the raw Altium pad
                # designators through pad_name_groups so a pad whose name
                # got disambiguated (one designator, several physical pads)
                # resolves to the same IR names the <smd>/<pad> elements use.
                out = []
                for raw in explicit[des]:
                    out.extend(pad_name_groups.get(raw, ()))
                return out
            return pad_name_groups.get(des, ())

        pm_el = ET.SubElement(fp_el, 'pin-mapping')
        if is_multi_gate:
            for part_idx in range(1, sym.part_count + 1):
                gate_name = _part_letter(part_idx)
                for ir_name, pin in _iter_named_pins(sym, part_idx):
                    names = _pads_for(pin)
                    if names:
                        ET.SubElement(pm_el, 'map', pin=f'{gate_name}.{ir_name}',
                                      pad=' '.join(names))
        else:
            for ir_name, pin in _iter_named_pins(sym):
                names = _pads_for(pin)
                if names:
                    ET.SubElement(pm_el, 'map', pin=ir_name, pad=' '.join(names))

        footprints.append((fp_name, fp_el))

    attrs = []
    for par in all_params:
        if par.name == 'Designator':
            continue
        # The value-bearing parameter and Comment are per-instance catalog
        # data for generic parts (R/C/...) — they live on the schematic
        # placement, not on the device record (Comment is Altium's
        # schematic-visible "value" field, see the >VALUE mapping above).
        # Baking the first catalog row's Resistance into the merged family
        # would stamp every variant "39.2k".
        if is_generic and par.name in ('Comment',) + _GENERIC_VALUE_PARAMS:
            continue
        if _is_param_alias(par, all_params):
            continue
        pname = clean_attr_name(par.name)
        if not pname:
            continue
        # IR attribute names are canonically lowercase (see
        # eagle_parser.convert_deviceset); Value/Comment both map onto the
        # same canonical 'value' key (see the >VALUE mapping above).
        key = 'value' if pname.lower() in ('value', 'comment') else pname.lower()
        attrs.append((key, par.text or ''))

    # pin DESIGNATOR -> IR pin name, straight off the authoritative symbol
    # via the same _iter_named_pins used to name pins everywhere else. A
    # project importer gets a pin's designator from the netlist terminal and
    # needs the IR name; it must not re-derive the '@N' dedup numbering
    # itself (that would be a second implementation free to drift), nor go
    # via the pad name — pin designator and pad name are only incidentally
    # equal, and an explicit MAP_DEFINER breaks that equality outright
    # (AMS1117 pin '4' -> pad '2', see _explicit_pin_map).
    pin_designators = {}
    if is_multi_gate:
        for part_idx in range(1, sym.part_count + 1):
            gate_name = _part_letter(part_idx)
            for ir_name, pin in _iter_named_pins(sym, part_idx):
                pin_designators[str(pin.designator)] = f'{gate_name}.{ir_name}'
    else:
        for ir_name, pin in _iter_named_pins(sym):
            pin_designators[str(pin.designator)] = ir_name

    return {
        'orig_name': comp.name,
        'prefix': _designator_prefix(sym),
        'is_generic': is_generic,
        'is_multi_gate': is_multi_gate,
        'symbol_name': canonical_sym_name,
        'symbol_el': sym_el_for_info,
        'gates': gates,
        'description': comp.description,
        'footprints': footprints,
        'pin_designators': pin_designators,
        'attrs': attrs,
    }


def convert_to_tree(intlib_path: str, models_dir, footprint_usage=None):
    """Parse .IntLib -> (lib_el, orig_to_compel) in memory, no file I/O for
    the XML itself (models are still extracted to `models_dir`, same as
    convert()). `orig_to_compel` maps the ORIGINAL IntLib component name to
    its <component> Element — this is exactly the by-name resolution table
    a project importer needs to look components up against (and hard-reject
    on a schematic reference that isn't in it, see
    [[project_altium_project_import]] "библиотека — единственный источник
    истины после Make Integrated Library"). Split out of convert() so a
    project importer can embed the SAME components/symbols this function
    builds, instead of re-parsing the serialized .swlib text back or
    building a second, divergent library representation.

    `footprint_usage` (project path only): {component name -> {footprint
    names}} collected from the SCHEMATIC. Altium keeps the symbol<->footprint
    association on the schematic instance, not in the library (no real
    library-level "device" concept, same as KiCad) — so a project import
    takes symbols and footprints from the IntLib but their PAIRING from the
    schematic. Keyed by BOTH `library_ref` and `design_item_id` by the
    caller, since which one the IntLib stores a component under varies (see
    [[project_altium_design_item_id]]). Omit it (pure library import) to use
    each component's own declared model list instead."""
    lib_path = Path(intlib_path)
    intlib   = AltiumIntLib(str(lib_path))

    tmpdir  = tempfile.mkdtemp()
    sources = intlib.extract_sources(tmpdir)

    schlib_cache = {'paths': {}, 'parsed': {}}
    pcblib_cache = {'paths': {}, 'parsed': {}}
    for src in sources.sources:
        if src.kind == 'SchLib':
            schlib_cache['paths'][src.stream_path] = src.output_path
        elif src.kind == 'PCBLib':
            pcblib_cache['paths'][src.stream_path] = src.output_path

    lib_el     = ET.Element('library', name=lib_path.stem, source='altium')
    symbols_el = ET.SubElement(lib_el, 'symbols')
    pool: dict = {}
    geom_pool: dict = {}   # geometry_hash -> canonical symbol name

    # Generic value-parametrized parts (R/C/...) get one IR <component> per
    # (prefix, symbol) — IntLib otherwise gives one row per catalog Value,
    # which is schematic-instance data, not a distinct device.
    # Keyed by (prefix, id(symbol_el)) rather than the symbol's name string:
    # the symbol gets renamed to the prefix below, and a name-based key would
    # go stale between the first group member (pre-rename) and the next one
    # (post-rename) sharing the same aliased symbol Element.
    generic_groups: dict = {}      # (prefix, id(symbol_el)) -> (comp_el, {fp_name: fp_el})
    orig_to_compel: dict = {}      # original IntLib component name -> its <component> Element
    pin_des_by_comp: dict = {}     # same key -> {pin designator: IR pin name}

    for comp in intlib.components:
        info = _convert_component(comp, schlib_cache, pcblib_cache,
                                   pool, symbols_el, geom_pool,
                                   footprint_names=(footprint_usage.get(comp.name)
                                                     if footprint_usage is not None else None))
        if info is None:
            continue

        if info['is_multi_gate']:
            comp_el = ET.Element('component', name=info['orig_name'], prefix=info['prefix'])
            # Altium has no on-sheet gate position to carry over (parts share
            # one symbol record, distinguished only by owner_part_id) — stack
            # the gates vertically, 0.5" apart, so identical-looking gates
            # (e.g. two diodes in one LED package) don't render on top of
            # each other at (0,0).
            for i, (gate_name, gate_sym_name) in enumerate(info['gates']):
                ET.SubElement(comp_el, 'gate', name=gate_name, symbol=gate_sym_name,
                              x='0', y=str(i * 12700))
            for fp_name, fp_el in info['footprints']:
                comp_el.append(fp_el)
            attrs_el = ET.SubElement(comp_el, 'attributes')
            # `description` is a plain attribute in IR (same as KiCad/Eagle),
            # not a dedicated element/component-attribute — kept first for
            # readability, no semantic significance to the order.
            if info['description']:
                ET.SubElement(attrs_el, 'attr', name='description',
                              value=info['description'], type='general')
            for aname, avalue in info['attrs']:
                ET.SubElement(attrs_el, 'attr', name=aname, value=avalue,
                              type='general')
            lib_el.append(comp_el)
            orig_to_compel[info['orig_name']] = comp_el
            pin_des_by_comp[info['orig_name']] = info['pin_designators']
        elif info['is_generic']:
            sym_el = info['symbol_el']
            prefix = info['prefix']
            key = (prefix, id(sym_el))
            if key not in generic_groups:
                # Rename the pooled symbol from its catalog-row name (R0603)
                # to the generic device name (R) — unless that name is
                # already taken by some unrelated symbol. A genuinely
                # different symbol under the same prefix (ground truth:
                # 8AO-VI's "C" — polarized vs non-polarized capacitor
                # bodies, real different geometry, not a dedup miss per the
                # user) can't ALSO claim the bare prefix as its <component
                # name> — that produced two same-named <component name="C">
                # elements, and every by-name lookup downstream silently
                # picked only one, dropping the other's instances' real
                # device. Per the user: keep such a component under its own
                # first catalog row's name instead of forcing the prefix.
                clash = symbols_el.find(f'symbol[@name="{prefix}"]')
                if clash is None or clash is sym_el:
                    sym_el.set('name', prefix)
                    comp_name = prefix
                else:
                    comp_name = info['orig_name']
                comp_el = ET.Element('component', name=comp_name,
                                      prefix=prefix, symbol=sym_el.get('name'))
                attrs_el = ET.SubElement(comp_el, 'attributes')
                if info['description']:
                    ET.SubElement(attrs_el, 'attr', name='description',
                                  value=info['description'], type='general')
                for aname, avalue in info['attrs']:
                    ET.SubElement(attrs_el, 'attr', name=aname, value=avalue,
                                  type='general')
                lib_el.append(comp_el)
                generic_groups[key] = (comp_el, {})
            comp_el, fp_map = generic_groups[key]
            for fp_name, fp_el in info['footprints']:
                if fp_name not in fp_map:
                    comp_el.append(fp_el)
                    fp_map[fp_name] = fp_el
            orig_to_compel[info['orig_name']] = comp_el
            pin_des_by_comp[info['orig_name']] = info['pin_designators']
        else:
            comp_el = ET.Element('component', name=info['orig_name'],
                                  prefix=info['prefix'], symbol=info['symbol_name'])
            for fp_name, fp_el in info['footprints']:
                comp_el.append(fp_el)
            attrs_el = ET.SubElement(comp_el, 'attributes')
            if info['description']:
                ET.SubElement(attrs_el, 'attr', name='description',
                              value=info['description'], type='general')
            for aname, avalue in info['attrs']:
                ET.SubElement(attrs_el, 'attr', name=aname, value=avalue,
                              type='general')
            lib_el.append(comp_el)
            orig_to_compel[info['orig_name']] = comp_el
            pin_des_by_comp[info['orig_name']] = info['pin_designators']

    _do_model_extraction(orig_to_compel, intlib, pcblib_cache, models_dir)
    return lib_el, orig_to_compel, pin_des_by_comp


def convert(intlib_path: str, output_path: str):
    """Convert .IntLib to IR XML file. Returns output_path."""
    lib_path = Path(intlib_path)
    models_dir = Path(output_path).parent / lib_path.stem
    lib_el, orig_to_compel, _pin_des = convert_to_tree(intlib_path, models_dir)

    raw   = minidom.parseString(ET.tostring(lib_el, encoding='unicode')).toprettyxml(indent='  ')
    clean = '\n'.join(l for l in raw.splitlines() if l.strip())
    xml   = '<?xml version="1.0" encoding="utf-8"?>\n' + '\n'.join(clean.splitlines()[1:])

    Path(output_path).write_text(xml, encoding='utf-8')
    n_comp = len(lib_el.findall('component'))
    print(f'Written: {output_path}  ({n_comp} components)')
    return output_path


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else \
        r'testData/GessorLib/Project Outputs for gessor_lib/gessor_lib.IntLib'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'testData/gessor.swlib'
    convert(src, dst)
