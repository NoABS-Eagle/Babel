import math
import re
import json
import shutil
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from babel.ir_util import resolve_model3d_file

# Eagle SCHEMATIC/SYMBOL layer number → IR layer name (visual grouping,
# ir_schema.md "Слои — единая номенклатура").
LAYER_MAP = {
    91: 'NETS',
    94: 'SYMBOLS',
    95: 'NAMES',
    96: 'VALUES',
    97: 'INFO',
}

# Eagle PACKAGE/BOARD layer number → IR signed layer number as a string
# (ir_schema.md "Плата (Board IR)", canonical constants in ir_util.py).
# Formula of recognizability: ours = Eagle + 100 for the top layer of a
# pair, the bottom counterpart is the NEGATIVE of that same number (bPlace
# 22 -> -121, not 122). tRestrict/bRestrict are not layers of their own —
# they dissolve into ANTI-copper of their side ('!1'/'!-1'). Unmapped
# system layers (tGlue/bGlue, tTest/bTest, tFinish/bFinish, Origins,
# Pads/Vias/Unrouted, vRestrict) are deliberate drops (junk/derived);
# user layers 100-255 (all unpaired in Eagle) go to +(n+100) standalone
# via _pkg_layer().
PKG_LAYER_MAP = {
    1:  '1',    16: '-1',
    20: '120',                 # Dimension (standalone)
    21: '121',  22: '-121',    # tPlace / bPlace
    25: '125',  26: '-125',    # tNames / bNames
    27: '127',  28: '-127',    # tValues / bValues
    29: '129',  30: '-129',    # tStop / bStop
    31: '131',  32: '-131',    # tCream / bCream
    39: '139',  40: '-139',    # tKeepout / bKeepout
    41: '!1',   42: '!-1',     # tRestrict / bRestrict -> anti-copper
    # 44 Drills / 45 Holes: display channels in Eagle too (drill renderings,
    # no stored objects) — no map entry; the impossible stray object would
    # fall through to +100 as a plain user layer. IR side: CHANNEL_DRILLS.
    46: '120',                 # Milling: cut, merged into 120 + PLATING copy below
    48: '148',                 # Document (standalone)
    156: '146',                # PLATING marker (canonical Eagle projection —
                               # Eagle has no native concept; 156 adopted from
                               # the user's real-board convention, must map
                               # back or 146 wouldn't round-trip). 146 = +100
                               # image of Milling 46 (PLATING marks plated
                               # milled edges); freed 147 for Measures 47.
    51: '151',  52: '-151',    # tDocu / bDocu
}


# Deliberate drops (eda_ir_design.md "Импорт ВЫБОРОЧНЫЙ"): derived layers
# (Pads/Vias are pad/via renderings, Unrouted is the ratsnest — connectivity
# lives in contactref) and junk (Origins, Finish, Glue, Test, orphan
# vRestrict). Everything NOT here and not in PKG_LAYER_MAP is carried as a
# standalone layer by the +100 formula — the rule is "objects survive unless
# the layer is known junk", not "only whitelisted layers survive" (real
# boards keep art on odd system-range layers: modtest.brd has 150 wires on
# a user-created layer 50 "dxf").
_PKG_LAYER_JUNK = {17, 18, 19, 23, 24, 33, 34, 35, 36, 37, 38, 43}

# Eagle Milling (46) means in practice "cut the fab treats as plated" (the
# very reason it is DRC-exempt there: copper must stay flush for plating to
# grow into). IR states that bit explicitly: the object becomes a cut on 120
# AND a marker copy on 146 PLATING (ir_schema.md "Резы и металлизация").
_PKG_LAYER_COPY = {46: '146'}


def _pkg_layer(eagle_n):
    """Eagle package/board layer number -> IR layer string, or None (drop)."""
    ir = PKG_LAYER_MAP.get(eagle_n)
    if ir is not None:
        return ir
    if eagle_n in _PKG_LAYER_JUNK or not 1 <= eagle_n <= 255:
        return None
    return str(eagle_n + 100)      # unpaired -> + standalone (user layers etc.)

DIR_MAP = {
    'in':  'in',
    'out': 'out',
    'io':  'io',
    'oc':  'io',
    'pwr': 'pwr',
    'pas': 'pas',
    'hiz': 'io',
    'sup': 'sup',
    'nc':  'nc',
}


def fmt(v):
    return f"{round(float(v), 6):g}"


def _um(mm):
    """mm string/float → integer µm string (rounds to nearest µm)."""
    return str(round(float(mm) * 1000))


def parse_rot(rot_str):
    """'MR90' → (90.0, True),  'R180' → (180.0, False),  None → (0.0, False)"""
    if not rot_str:
        return 0.0, False
    s = rot_str.strip()
    mirrored = 'M' in s
    angle = float(re.sub(r'[MRS]', '', s) or '0')
    return angle, mirrored


# (eagle_arc is gone: the IR arc canon IS Eagle's endpoint form — see
# ir_util's arc-math block; center math lives there as arc_center/arc_params
# for the consumers that still need it.)


def _eagle_to_mcad_rot(rx_e, ry_e, rz_e):
    """Convert Eagle intrinsic XYZ angles to MCAD intrinsic XYZ.

    Eagle stores R_eagle = Rx·Ry·Rz (same intrinsic order as MCAD/Altium),
    with an implicit Rx(90°) frame correction applied on the right:
    R_mcad = R_eagle·Rx(-90°), decomposed as Rx(rx)·Ry(ry)·Rz(rz).
    Reverse-engineered from ground-truth (rx_e,ry_e,rz_e) -> (rx_m,ry_m,rz_m)
    pairs read directly out of Altium's 3D Body properties panel.
    """
    from math import radians, degrees, cos, atan2, asin

    def _rx(a): c,s=cos(a),__import__('math').sin(a); return [[1,0,0],[0,c,-s],[0,s,c]]
    def _ry(a): c,s=cos(a),__import__('math').sin(a); return [[c,0,s],[0,1,0],[-s,0,c]]
    def _rz(a): c,s=cos(a),__import__('math').sin(a); return [[c,-s,0],[s,c,0],[0,0,1]]
    def _mul(A,B): return [[sum(A[i][k]*B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    d = radians
    R_eagle = _mul(_mul(_rx(d(rx_e)), _ry(d(ry_e))), _rz(d(rz_e)))
    M = _mul(R_eagle, _rx(d(-90)))

    ry_m = asin(max(-1.0, min(1.0, M[0][2])))
    if abs(cos(ry_m)) > 1e-6:
        rz_m = atan2(-M[0][1], M[0][0])
        rx_m = atan2(-M[1][2], M[2][2])
    else:
        rz_m = atan2(M[1][0], M[1][1])
        rx_m = 0.0

    def _clean(rad):
        v = degrees(rad)
        v = v % 360
        if v > 180: v -= 360
        if abs(v) < 1e-9: v = 0.0
        return int(v) if v == int(v) else round(v, 4)

    return _clean(rx_m), _clean(ry_m), _clean(rz_m)


def parse_3d(desc):
    """Extract 3D placement dict from Eagle description comment <!--3d:{...}-->."""
    if not desc:
        return None
    m = re.search(r'<!--3d:(\{[^}]+\})-->', desc)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Symbol conversion
# ---------------------------------------------------------------------------

def convert_symbol(sym_el, sym_name):
    """Convert Eagle <symbol> to IR <symbol name="..."> for the library pool."""
    sym = ET.Element('symbol')
    sym.set('name', sym_name)

    for child in sym_el:
        tag = child.tag
        layer = int(child.get('layer', 0))
        # Eagle lets geometry/text sit on ANY layer inside a <symbol> (real
        # Eagle, confirmed by the user drawing a rectangle on Names/95
        # directly) — no layer filters what's a valid symbol object, so
        # every wire/rectangle/circle/polygon/text is imported regardless
        # of its layer number, carrying that layer's IR name through
        # explicitly (falling back to the raw Eagle number as a string for
        # anything not in LAYER_MAP, same as ir_schema.md "Слои": an
        # unrecognized name is not an error).
        ir_layer = LAYER_MAP.get(layer, str(layer))

        if tag == 'wire':
            # IR arc canon = Eagle's own endpoint form (x1 y1 x2 y2 curve,
            # ir_util arc-math comment) — a curved wire converts with NO
            # geometry math at all, endpoints stay lattice-exact
            curve = float(child.get('curve', 0))
            el = ET.SubElement(sym, 'arc' if curve else 'line')
            el.set('x1', _um(child.get('x1'))); el.set('y1', _um(child.get('y1')))
            el.set('x2', _um(child.get('x2'))); el.set('y2', _um(child.get('y2')))
            if curve:
                el.set('curve', fmt(curve))
            el.set('width', _um(child.get('width', '0.1524')))
            el.set('layer', ir_layer)

        elif tag == 'rectangle':
            x1, y1 = float(child.get('x1')), float(child.get('y1'))
            x2, y2 = float(child.get('x2')), float(child.get('y2'))
            rot, _ = parse_rot(child.get('rot'))
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            w, h = abs(x2 - x1), abs(y2 - y1)
            if round(rot) % 180 == 90:
                w, h = h, w
                rot = rot - 90
            el = ET.SubElement(sym, 'shape')
            el.set('x', str(round(cx * 1000))); el.set('y', str(round(cy * 1000)))
            el.set('w', str(round(w * 1000))); el.set('h', str(round(h * 1000)))
            el.set('roundness', '0')
            el.set('rot', str(round(rot)))
            el.set('outline', _um(child.get('width', '0')))
            el.set('layer', ir_layer)

        elif tag == 'circle':
            r_um = round(float(child.get('radius')) * 1000)
            el = ET.SubElement(sym, 'shape')
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('w', str(r_um * 2)); el.set('h', str(r_um * 2))
            el.set('roundness', '100')
            el.set('rot', '0')
            el.set('outline', _um(child.get('width', '0')))
            el.set('layer', ir_layer)

        elif tag == 'polygon':
            width_um = round(float(child.get('width', '0')) * 1000)
            if 0 < width_um < 50:
                width_um = 50
            pg = ET.SubElement(sym, 'polygon')
            pg.set('width', str(width_um))
            pg.set('layer', ir_layer)
            for v in child.findall('vertex'):
                ve = ET.SubElement(pg, 'vertex')
                ve.set('x', str(round(float(v.get('x', '0')) * 1000)))
                ve.set('y', str(round(float(v.get('y', '0')) * 1000)))

        elif tag == 'text':
            rot, _ = parse_rot(child.get('rot'))
            el = ET.SubElement(sym, 'text')
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('size', _um(child.get('size')))
            el.set('rot', fmt(rot))
            el.set('align', child.get('align', 'bottom-left'))
            el.set('layer', ir_layer)
            if child.get('font') == 'vector':
                el.set('font', 'vector')
            if child.get('ratio'):
                el.set('ratio', child.get('ratio'))
            el.text = child.text or ''

        elif tag == 'frame':
            # ir_schema.md "Frame": a Frame-role symbol carries exactly one
            # <shape> on the FRAME layer (bounding rectangle). columns/rows
            # — the reference-zone grid (A1/B2 lookup zones, ISO 5457) —
            # are CARRIED as optional shape attrs (revised 2026-07-10: a
            # general drawing concept, Altium has sheet zones too, not
            # Eagle decoration); border-* toggles stay dropped. This is Eagle's SECOND way to place a frame
            # (the first being a bare <frame> directly in a sheet's <plain>,
            # handled separately by the schematic importer, not here) — a
            # stock "Frame" library deviceset (e.g. frames.lbr) whose SYMBOL
            # itself contains a <frame> element; confirmed real
            # (testData/maximus.sch, <symbol name="A3L-LOC">, no pins).
            x1, y1 = float(child.get('x1')), float(child.get('y1'))
            x2, y2 = float(child.get('x2')), float(child.get('y2'))
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            w, h = abs(x2 - x1), abs(y2 - y1)
            el = ET.SubElement(sym, 'shape')
            el.set('x', str(round(cx * 1000))); el.set('y', str(round(cy * 1000)))
            el.set('w', str(round(w * 1000))); el.set('h', str(round(h * 1000)))
            el.set('roundness', '0')
            el.set('rot', '0')
            el.set('outline', _um(child.get('width', '0')))
            el.set('layer', 'FRAME')
            # cartouche grid — needed to re-emit the symbol's <frame>
            if child.get('columns'):
                el.set('columns', child.get('columns'))
            if child.get('rows'):
                el.set('rows', child.get('rows'))

        elif tag == 'pin':
            direction = child.get('direction', 'io')
            ir_dir = DIR_MAP.get(direction, 'io')
            # Eagle's `visible` (off/pad/pin/both) controls ONLY the name/
            # number LABELS, never the pin stub itself — Eagle always draws
            # the pin body regardless (per the user, confirmed against the
            # Eagle manual's own Direction section: `visible` is a separate
            # axis entirely from `direction`/`nc`). Unlike KiCad's `(hide
            # yes)` (which suppresses the WHOLE pin, stub included — see
            # kicad_parser.py's "no_connect + hidden -> skip entirely"), an
            # Eagle `nc` pin is ALWAYS imported as a real <pin>, visible or
            # not — its own pinvis/padvis booleans (below) already capture
            # whatever label visibility the source actually has.
            rot, _ = parse_rot(child.get('rot'))
            pin_len_um = {'point': 0, 'short': 2540, 'middle': 5080, 'long': 7620}.get(
                child.get('length', 'long'), 7620)
            el = ET.SubElement(sym, 'pin')
            el.set('name', child.get('name'))
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('rot', fmt(rot))
            el.set('length', str(pin_len_um))
            el.set('direction', ir_dir)
            el.set('function', child.get('function', 'none'))
            # Eagle's single combined enum (off/pad/pin/both — 'pin' = name
            # shown, 'pad' = number shown) split into two independent IR
            # booleans, always written explicitly (per the user).
            eagle_visible = child.get('visible', 'both')
            el.set('pinvis', '1' if eagle_visible in ('both', 'pin') else '0')
            el.set('padvis', '1' if eagle_visible in ('both', 'pad') else '0')

    return sym


# ---------------------------------------------------------------------------
# Footprint (package) conversion
# ---------------------------------------------------------------------------

def convert_geometry_mapped(child, parent):
    """convert_geometry with the layer mapping applied from the child's own
    Eagle layer number — the entry point for package/board drawing children.
    Also the single home of the _PKG_LAYER_COPY rule: Eagle Milling (46)
    yields TWO IR objects, the cut on 120 and its marker copy on 146 PLATING."""
    raw = child.get('layer')
    eagle_n = int(raw) if raw else None
    ir_layer = _pkg_layer(eagle_n) if eagle_n is not None else None
    handled = convert_geometry(child, parent, ir_layer)
    copy_layer = _PKG_LAYER_COPY.get(eagle_n)
    if handled and copy_layer is not None:
        convert_geometry(child, parent, copy_layer)
    return handled


def convert_geometry(child, parent, ir_layer):
    """One Eagle drawing primitive (wire/circle/rectangle/polygon/text/hole)
    -> IR element appended to `parent` with layer=ir_layer. The SINGLE home
    for this conversion — used by convert_package (package geometry) and by
    eagle_board_parser (board <plain> free geometry): same Eagle tags, same
    IR primitives, one implementation. Returns True if the tag was handled
    (regardless of whether ir_layer was None and it got dropped)."""
    tag = child.tag

    if tag == 'wire':
        if ir_layer is None:
            return True
        # arc canon = Eagle's endpoint form: no geometry math, endpoints
        # stay lattice-exact (ir_util arc-math comment)
        curve = float(child.get('curve', 0))
        el = ET.SubElement(parent, 'arc' if curve else 'line')
        el.set('x1', _um(child.get('x1'))); el.set('y1', _um(child.get('y1')))
        el.set('x2', _um(child.get('x2'))); el.set('y2', _um(child.get('y2')))
        if curve:
            el.set('curve', fmt(curve))
        el.set('width', _um(child.get('width', '0.1524')))
        el.set('layer', ir_layer)
        return True

    if tag == 'circle':
        if ir_layer is None:
            return True
        r_um = round(float(child.get('radius')) * 1000)
        el = ET.SubElement(parent, 'shape')
        el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
        el.set('w', str(r_um * 2)); el.set('h', str(r_um * 2))
        el.set('roundness', '100')
        el.set('rot', '0')
        el.set('outline', _um(child.get('width', '0')))
        el.set('layer', ir_layer)
        return True

    if tag == 'rectangle':
        if ir_layer is None:
            return True
        x1, y1 = float(child.get('x1')), float(child.get('y1'))
        x2, y2 = float(child.get('x2')), float(child.get('y2'))
        rot, _ = parse_rot(child.get('rot'))
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        w, h = abs(x2 - x1), abs(y2 - y1)
        if round(rot) % 180 == 90:
            w, h = h, w
            rot = rot - 90
        el = ET.SubElement(parent, 'shape')
        el.set('x', str(round(cx * 1000))); el.set('y', str(round(cy * 1000)))
        el.set('w', str(round(w * 1000))); el.set('h', str(round(h * 1000)))
        el.set('roundness', '0')
        el.set('rot', str(round(rot)))
        el.set('outline', _um(child.get('width', '0')))
        el.set('layer', ir_layer)
        return True

    if tag == 'polygon':
        if ir_layer is None:
            return True
        width_um = round(float(child.get('width', '0')) * 1000)
        if 0 < width_um < 50:
            width_um = 50
        pg = ET.SubElement(parent, 'polygon')
        pg.set('width', str(width_um))
        pg.set('layer', ir_layer)
        for v in child.findall('vertex'):
            ve = ET.SubElement(pg, 'vertex')
            ve.set('x', str(round(float(v.get('x', '0')) * 1000)))
            ve.set('y', str(round(float(v.get('y', '0')) * 1000)))
            if v.get('curve'):
                # arc to the NEXT vertex, degrees CCW+ (ir_schema.md <polygon>)
                ve.set('curve', fmt(v.get('curve')))
        return True

    if tag == 'text':
        if ir_layer is None:
            return True
        rot, mirror = parse_rot(child.get('rot'))
        el = ET.SubElement(parent, 'text')
        if mirror:
            el.set('mirror', '1')     # reading-direction flip (bottom-side texts)
            # Eagle MR{α} = rotate α, THEN mirror; IR text semantics is
            # mirror-then-rotate (svg_renderer transform order, KiCad
            # mirrored-text encoding, place_ir_element — all agree) — the
            # equivalent IR angle is −α. Same convention bug family as
            # element MR rotation (tolmach ground truth: bottom 90° texts
            # sat 180° off in a live KiCad render).
            rot = (-rot) % 360
        el.set('layer', ir_layer)
        el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
        el.set('size', _um(child.get('size')))
        el.set('rot', fmt(rot))
        el.set('align', child.get('align', 'bottom-left'))
        if child.get('font') == 'vector':
            el.set('font', 'vector')
        if child.get('ratio'):
            el.set('ratio', child.get('ratio'))
        el.text = child.text or ''
        return True

    if tag == 'hole':
        # layer-less by nature (through the whole stack)
        el = ET.SubElement(parent, 'hole')
        el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
        el.set('drill', _um(child.get('drill')))
        return True

    return False


def convert_package(pkg_el, pkg_name):
    fp = ET.Element('footprint')
    fp.set('name', pkg_name)

    desc_el = pkg_el.find('description')
    desc_text = (desc_el.text or '') if desc_el is not None else ''
    clean_desc = re.sub(r'\s*<!--3d:\{[^}]*\}-->', '', desc_text).strip()
    if clean_desc:
        d = ET.SubElement(fp, 'description')
        d.text = clean_desc

    for child in pkg_el:
        tag = child.tag
        layer = int(child.get('layer', 0))
        ir_layer = _pkg_layer(layer)

        if tag == 'smd':
            # <smd> carries no layer attr for the normal mount-side pad
            # (copper by construction); a Bottom(16) pad in a library
            # package keeps its far-sidedness via explicit layer="-1".
            if ir_layer not in ('1', '-1'):
                continue
            rot, _ = parse_rot(child.get('rot'))
            el = ET.SubElement(fp, 'smd')
            if ir_layer == '-1':
                el.set('layer', '-1')
            el.set('name', child.get('name'))
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('width', str(round(float(child.get('dx')) * 1000)))
            el.set('height', str(round(float(child.get('dy')) * 1000)))
            el.set('roundness', child.get('roundness', '0'))
            if rot:
                el.set('rot', fmt(rot))

        elif tag == 'pad':
            shape = child.get('shape', 'round')
            ir_shape = 'square' if shape == 'square' else 'round'
            el = ET.SubElement(fp, 'pad')
            el.set('name', child.get('name'))
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('drill', _um(child.get('drill')))
            if child.get('diameter'):
                el.set('diameter', _um(child.get('diameter')))
            el.set('shape', ir_shape)

        elif tag in ('hole', 'wire', 'circle', 'rectangle', 'polygon', 'text'):
            convert_geometry_mapped(child, fp)

    d3 = parse_3d(desc_text)
    if d3:
        m3d = ET.SubElement(fp, 'model3d')
        m3d.set('tx', str(round(float(d3.get('tx', 0)) * 1000)))
        m3d.set('ty', str(round(float(d3.get('ty', 0)) * 1000)))
        m3d.set('tz', str(round(float(d3.get('tz', 0)) * 1000)))
        rx_m, ry_m, rz_m = _eagle_to_mcad_rot(
            float(d3.get('rx', 0)), float(d3.get('ry', 0)), float(d3.get('rz', 0)))
        m3d.set('rx', fmt(rx_m))
        m3d.set('ry', fmt(ry_m))
        m3d.set('rz', fmt(rz_m))

    return fp


# ---------------------------------------------------------------------------
# Component (deviceset) conversion
# ---------------------------------------------------------------------------

def collect_attributes(ds_el):
    attrs = {}
    for tech in ds_el.iter('technology'):
        for attr in tech.findall('attribute'):
            name = attr.get('name')
            val  = attr.get('value', '')
            if name not in attrs or (not attrs[name] and val):
                attrs[name] = val
    return attrs


def convert_deviceset(ds_el, packages):
    """One Eagle <deviceset> -> a LIST of IR <component> elements, one per
    distinct technology name found across its devices.

    Eagle's <technology> (per-device, e.g. "-1%"/"-5%" tolerance variants
    on the SAME footprint) has no IR equivalent — IR is two-level
    (component -> footprint/variant), Eagle is three-level (deviceset ->
    device -> technology). Per the user: this is a deliberate, honest
    simplification, not a loss to patch around — "физически разные
    устройства" (a 1.8V and a 3.3V regulator ARE two different real parts,
    e.g. crystal:CRYSTAL-4P's "-16MHZ"/"-24MHZ" devices, or linear
    regulators:XC6206P's "182"/"302"/"332" output-voltage variants) deserve
    to be modeled as independent components, not squeezed into one with a
    fake "technology" axis nobody but Eagle itself tracks. The previous
    behavior (collapsing every technology's attributes into ONE merged set
    per device, first non-empty value silently winning) was found to
    actively corrupt device-specific facts on testData/maximus.sch's own
    round-trip: "R" device "-0603"'s "-1%" and "-5%" technology variants
    share one footprint but carry DIFFERENT MANF#/LCSC# per technology —
    merging them let one technology's attributes silently overwrite the
    other's on export.

    One IR <component> per distinct technology NAME across ALL of this
    deviceset's devices (not per-device — Eagle only bothers naming a
    technology when it's a REAL, meaningfully different variant, and the
    same name means the same real thing across devices that share it,
    confirmed on "R": every device with a "-1%"/"-5%" pair names them
    identically). Naming: bare `name=""` -> the deviceset's own name
    unchanged (e.g. "R") — the ordinary case for devices that only ever
    have ONE anonymous technology (most Eagle libraries; "R"'s own SQP-5W/
    -0201 devices fall in this bucket alongside every "normal" component
    with no tolerance variants at all). A REAL non-empty technology name
    suffixes the deviceset name directly (Eagle's own convention already
    embeds any separator it wants in the name itself, e.g. "-1%" already
    starts with "-": ds_name + tech_name, no separator inserted here) ->
    "R-1%", "R-5%". A device that has NO technology matching a given name
    at all (e.g. "-SQP-5W" only ever has "") simply isn't included in
    that name's component — <component name="R-1%"> only carries the 7
    devices that actually declare a "-1%" technology, not all 9.

    Each returned <component>'s name is unique — the caller registers each
    independently (same shape a single-technology deviceset already
    produced, just N times instead of once).
    """
    tech_names = set()
    for device in ds_el.find('devices').findall('device'):
        for tech in device.findall('technologies/technology'):
            tech_names.add(tech.get('name', ''))
    if not tech_names:
        tech_names = {''}

    ds_name = ds_el.get('name')
    gates_el = ds_el.find('gates')
    gate_list = gates_el.findall('gate') if gates_el is not None else []
    multi = len(gate_list) > 1

    components = []
    for tech_name in sorted(tech_names):
        comp = ET.Element('component')
        comp.set('name', ds_name + tech_name if tech_name else ds_name)
        if ds_el.get('prefix'):
            comp.set('prefix', ds_el.get('prefix'))
        if ds_el.get('uservalue') == 'yes':
            comp.set('uservalue', 'yes')

        attrs_el = ET.SubElement(comp, 'attributes')
        ET.SubElement(attrs_el, 'attr').attrib.update({'name': 'value', 'value': ''})
        # `description` is a plain attribute in IR, same as everywhere else
        # (KiCad/Altium) — no dedicated <description> element. Eagle's own
        # native deviceset <description> is special-cased back out of this
        # attr only on export (see eagle_exporter.export_deviceset), not
        # here — same text on every technology-split component (it's a
        # deviceset-level fact, not per-technology).
        desc_el = ds_el.find('description')
        if desc_el is not None and desc_el.text:
            ET.SubElement(attrs_el, 'attr').attrib.update(
                {'name': 'description', 'value': desc_el.text})

        if not multi and gate_list:
            comp.set('symbol', gate_list[0].get('symbol', ''))
        else:
            for gate in gate_list:
                g = ET.SubElement(comp, 'gate')
                g.set('name', gate.get('name'))
                g.set('symbol', gate.get('symbol', ''))
                g.set('x', str(round(float(gate.get('x', '0')) * 1000)))
                g.set('y', str(round(float(gate.get('y', '0')) * 1000)))

        for device in ds_el.find('devices').findall('device'):
            pkg_name = device.get('package')
            if not pkg_name or pkg_name not in packages:
                continue

            tech_el = next((t for t in device.findall('technologies/technology')
                             if t.get('name', '') == tech_name), None)
            if tech_el is None:
                continue   # this device has no variant under this technology name

            fp = convert_package(packages[pkg_name], pkg_name)
            dev_variant = device.get('name', '')
            if dev_variant:
                fp.set('variant', dev_variant)

            # This technology's OWN attributes only — no merging across
            # different technology names (that was the bug being fixed).
            dev_attrs: dict[str, str] = {}
            for attr in tech_el.findall('attribute'):
                n = attr.get('name', '').lower()
                v = attr.get('value', '')
                if n and v:
                    dev_attrs[n] = v
            if dev_attrs:
                fa = ET.SubElement(fp, 'attributes')
                for n, v in dev_attrs.items():
                    ET.SubElement(fa, 'attr').attrib.update({'name': n, 'value': v})

            connects = device.find('connects')
            if connects is not None:
                pm = ET.SubElement(fp, 'pin-mapping')
                for conn in connects.findall('connect'):
                    m = ET.SubElement(pm, 'map')
                    m.set('pad', conn.get('pad'))
                    if multi:
                        m.set('pin', f"{conn.get('gate')}.{conn.get('pin')}")
                    else:
                        m.set('pin', conn.get('pin'))

            comp.append(fp)

        components.append(comp)

    return components


# ---------------------------------------------------------------------------
# Top-level library conversion
# ---------------------------------------------------------------------------

def convert(lbr_path, output_path=None):
    tree = ET.parse(lbr_path)
    root = tree.getroot()
    lib_el = root.find('.//library')

    packages = {p.get('name'): p for p in lib_el.find('packages').findall('package')}
    eagle_syms = {s.get('name'): s for s in lib_el.find('symbols').findall('symbol')}

    lib = ET.Element('library')
    lib.set('name', Path(lbr_path).stem)

    # Symbol pool — all symbols defined in the library
    symbols_el = ET.SubElement(lib, 'symbols')
    for sym_name, sym_el in eagle_syms.items():
        symbols_el.append(convert_symbol(sym_el, sym_name))

    # Components from devicesets — one deviceset -> N components, one per
    # distinct Eagle <technology> name (see convert_deviceset).
    used_pkg_names = set()
    for ds in lib_el.find('devicesets').findall('deviceset'):
        for comp in convert_deviceset(ds, packages):
            lib.append(comp)
        for device in ds.find('devices').findall('device'):
            pkg = device.get('package', '')
            if pkg:
                used_pkg_names.add(pkg)

    # Orphaned packages (defined but not referenced by any deviceset)
    for pkg_name, pkg_el in packages.items():
        if pkg_name not in used_pkg_names:
            lib.append(convert_package(pkg_el, pkg_name))

    # 3D model files: attach file= attrs to <model3d> BEFORE serializing to XML
    if output_path:
        _copy_step_files(lib, Path(lbr_path), Path(output_path))

    xml_str = minidom.parseString(ET.tostring(lib, encoding='unicode')) \
                     .toprettyxml(indent='  ')
    lines = xml_str.splitlines()
    clean = '\n'.join(l for l in lines if l.strip())
    result = '<?xml version="1.0" encoding="utf-8"?>\n' + \
             '\n'.join(clean.splitlines()[1:])

    if output_path:
        Path(output_path).write_text(result, encoding='utf-8')
        print(f"Written: {output_path}")
    return result


def _copy_step_files(lib_el, lbr_path, out_path):
    """Find STEP/WRL files in <lbr_stem>/ next to lbr, copy to <out_stem>/ next to IR."""
    step_src = lbr_path.parent / lbr_path.stem
    if not step_src.is_dir():
        return
    step_dst = out_path.parent / out_path.stem

    def _attach(fp_el):
        src = resolve_model3d_file(fp_el, step_src)
        if src is not None:
            step_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, step_dst / src.name)
            print(f'  3D: {src.name}')

    for comp in lib_el.findall('component'):
        for fp_el in comp.findall('footprint'):
            _attach(fp_el)
    for fp_el in lib_el.findall('footprint'):
        _attach(fp_el)


if __name__ == '__main__':
    import sys
    lbr = sys.argv[1] if len(sys.argv) > 1 else 'testData/rc.lbr'
    out = sys.argv[2] if len(sys.argv) > 2 else 'testData/rc.swlib'
    convert(lbr, out)
