"""IR <layout> -> KiCad 10 .kicad_pcb (topology milestone).

Carries: board outline / free geometry, placed footprints with pads bound
to nets, tracks (line/arc), through vias, layout-level mounting holes,
3D models (embedded into the .kicad_pcb, KiCad's "embed file" feature —
the same form kicad_project_parser requires on import, so our own output
round-trips; sidecar STEP files come from <ir_stem>/ next to the IR).
Deferred, NEVER silently: pours -> zones, anti-copper -> keepouts,
element-level text overrides (smashed).

Skeleton and dialect are ground truth from KiCad 10 files (testData/Simple
v10 header/layers/setup; testData/video v10 net-by-name on segments/pads —
the numbered net table died with v9, we don't emit one).

Bottom-side baking (KiCad stores footprints flip-BAKED; the "flip
top/bottom" convention, matching native-board evidence — vimdrones pad
angles 90->270, Pocket-Lab text anchors):
  top:    local (x, -y),  at-rot = θ,        item angle = θ + φ
  bottom: local (x,  y),  at-rot = θ + 180,  item angle = θ - φ,
          layer names F<->B swapped, texts get (justify mirror)
Derived from the IR placement canon (ir_util.place_ir_element: bottom =
mirror x in footprint frame, then rotate) mapped through KiCad's render
model; verified pad-for-pad by verify_kicad_board.py against
place_ir_element as the oracle.
"""
import math
import base64
import re
import struct
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import zstandard

from babel import import_log
from babel.eagle_board_exporter import _instance_footprint
from babel.ir_util import (arc_mid, is_copper, offset_contour, parse_layer,
                           parse_stack, place_ir_element, sanitize_filename,
                           instance_designator, resolve_model3d_file)
from babel.kicad_layers import ir_to_kicad
from babel.kicad_exporter import (_eagle_overbar_to_kicad, _f, _justify,
                                  _mm, _q, model3d_kicad_xyz)

_VERSION = 20260206          # KiCad 10 board dialect (testData/Simple)

# Standard KiCad 10 layer table (ids ground truth: testData/Simple). Copper
# ids are assigned dynamically (F.Cu=0, B.Cu=2, In<k>.Cu=4,6,...).
_STD_LAYERS = [
    (9, 'F.Adhes', 'user', 'F.Adhesive'),
    (11, 'B.Adhes', 'user', 'B.Adhesive'),
    (13, 'F.Paste', 'user', None),
    (15, 'B.Paste', 'user', None),
    (5, 'F.SilkS', 'user', 'F.Silkscreen'),
    (7, 'B.SilkS', 'user', 'B.Silkscreen'),
    (1, 'F.Mask', 'user', None),
    (3, 'B.Mask', 'user', None),
    (17, 'Dwgs.User', 'user', 'User.Drawings'),
    (19, 'Cmts.User', 'user', 'User.Comments'),
    (21, 'Eco1.User', 'user', 'User.Eco1'),
    (23, 'Eco2.User', 'user', 'User.Eco2'),
    (25, 'Edge.Cuts', 'user', None),
    (27, 'Margin', 'user', None),
    (31, 'F.CrtYd', 'user', 'F.Courtyard'),
    (29, 'B.CrtYd', 'user', 'B.Courtyard'),
    (35, 'F.Fab', 'user', None),
    (33, 'B.Fab', 'user', None),
]

def _uuid(*key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          'babel-board:' + ':'.join(str(k) for k in key)))


def _copper_name(n, n_copper):
    if n == 1:
        return 'F.Cu'
    if n == -1:
        return 'B.Cu'
    return f'In{n - 1}.Cu'


def _board_layer_name(ir_layer, log_ctx):
    """Absolute IR board layer -> KiCad name, or None (logged by caller).
    Non-copper projection is the user-editable table (babel/kicad_layers.py);
    inner copper is numbered from the stack, so it stays in code."""
    anti, n = parse_layer(ir_layer)
    if anti:
        return None
    if is_copper(n):
        return None if abs(n) > 64 else _copper_name(n, None)
    return ir_to_kicad(n)


def _fp_layer_name(ir_layer, bottom):
    """Footprint-local IR layer (sign = mount side) -> KiCad name on the
    given side, or None. Anti layers have no footprint equivalent (drop,
    caller logs)."""
    try:
        anti, n = parse_layer(ir_layer)
    except (TypeError, ValueError):
        return None
    if anti:
        return None
    if bottom:
        n = -n
    if is_copper(n):
        return 'F.Cu' if n > 0 else 'B.Cu'
    # paired layers carry their side in the sign; standalone layers (no side)
    # are stored positive-only, so an unflipped fallback resolves them
    return ir_to_kicad(n) or (ir_to_kicad(-n) if n < 0 else None)


class _Frame:
    """IR µm Y-up -> KiCad mm Y-down page frame. The offset parks the board
    fully in positive page coordinates (deterministic: bbox min corner at
    ~(25, 25) mm, whole-mm offset so coordinates stay readable)."""

    def __init__(self, min_x_um, max_y_um):
        self.ox = 25 - math.floor(min_x_um / 1000)
        self.oy = 25 + math.ceil(max_y_um / 1000)

    def x(self, x_um):
        return float(x_um) / 1000 + self.ox

    def y(self, y_um):
        return self.oy - float(y_um) / 1000


def _font(size_um, ratio, width_um=None):
    """KiCad (size HEIGHT WIDTH) — order is ground truth from pcbnew
    (SetTextSize(w=0.8, h=2.0) -> '(size 2 0.8)'). Width has NO default in
    KiCad (0 renders nothing): absent IR width = the canonical renderer
    aspect 0.85*height (ir_schema.md <text>)."""
    h = _mm(str(size_um))
    w = _mm(str(width_um)) if width_um else h * 0.85
    th = h * int(ratio or '8') / 100
    return f'(font (size {_f(h)} {_f(w)}) (thickness {_f(th)}))'


def _pad_lines(el, bottom, net, abs_rot, log):
    """One IR pad-ish child (smd/pad/hole) -> board-form pad s-expr lines.
    abs_rot: the angle KiCad stores on pads is ABSOLUTE (footprint rotation
    already folded in — ground truth video.kicad_pcb fp@90/pad@90)."""
    t = el.tag
    x, y = _mm(el.get('x', '0')), _mm(el.get('y', '0'))
    ly = y if bottom else -y
    net_s = f'\n\t\t\t(net {net})' if net else ''
    if t == 'smd':
        far = el.get('layer') == '-1'
        side = 'B' if (far != bottom) else 'F'
        w, h = _mm(el.get('width')), _mm(el.get('height'))
        rnd = float(el.get('roundness', 0)) / 200
        shape = 'roundrect' if rnd > 0 else 'rect'
        extra = f'\n\t\t\t(roundrect_rratio {_f(rnd)})' if rnd > 0 else ''
        return [f'\t\t(pad {_q(el.get("name", ""))} smd {shape}',
                f'\t\t\t(at {_f(x)} {_f(ly)} {_f(abs_rot % 360)})',
                f'\t\t\t(size {_f(w)} {_f(h)})',
                f'\t\t\t(layers "{side}.Cu" "{side}.Paste" "{side}.Mask")'
                + extra + net_s,
                f'\t\t)']
    if t == 'pad':
        drill = _mm(el.get('drill', '1000'))
        if el.get('diameter'):
            od = _mm(el.get('diameter'))
        else:
            # diameter is mandatory (ir_schema.md "DRC-ядро") — stale IR only
            od = drill * 1.8
            log('PAD_DIAMETER missing', f'synthesized {od:g}mm (1.8x drill)')
        kshape = 'rect' if el.get('shape') == 'square' else 'circle'
        return [f'\t\t(pad {_q(el.get("name", ""))} thru_hole {kshape}',
                f'\t\t\t(at {_f(x)} {_f(ly)} {_f(abs_rot % 360)})',
                f'\t\t\t(size {_f(od)} {_f(od)})',
                f'\t\t\t(drill {_f(drill)})',
                f'\t\t\t(layers "*.Cu" "*.Mask")' + net_s,
                f'\t\t)']
    # hole
    d = _mm(el.get('drill'))
    return [f'\t\t(pad "" np_thru_hole circle',
            f'\t\t\t(at {_f(x)} {_f(ly)})',
            f'\t\t\t(size {_f(d)} {_f(d)})',
            f'\t\t\t(drill {_f(d)})',
            f'\t\t\t(layers "*.Cu" "*.Mask")',
            f'\t\t)']


def _fp_geometry_lines(el, bottom, theta, log):
    """One IR footprint graphic child -> fp_* s-expr lines (board form).
    Local coords: top (x, -y), bottom (x, y) — the T/B flip bake."""
    kl = _fp_layer_name(el.get('layer'), bottom)
    if kl is None:
        log('FP_GEOMETRY dropped', f'{el.tag} layer={el.get("layer")}')
        return []
    sy = 1 if bottom else -1
    t = el.tag
    if t == 'line':
        x1, y1 = _mm(el.get('x1')), sy * _mm(el.get('y1'))
        x2, y2 = _mm(el.get('x2')), sy * _mm(el.get('y2'))
        w = _mm(el.get('width', '120'))
        return [f'\t\t(fp_line',
                f'\t\t\t(start {_f(x1)} {_f(y1)}) (end {_f(x2)} {_f(y2)})',
                f'\t\t\t(stroke (width {_f(w)}) (type solid))',
                f'\t\t\t(layer "{kl}")',
                f'\t\t)']
    if t == 'arc':
        x1, y1 = _mm(el.get('x1')), _mm(el.get('y1'))
        x2, y2 = _mm(el.get('x2')), _mm(el.get('y2'))
        mx, my = arc_mid(x1, y1, x2, y2, float(el.get('curve', '0')))
        w = _mm(el.get('width', '120'))
        return [f'\t\t(fp_arc',
                f'\t\t\t(start {_f(x1)} {_f(sy * y1)}) '
                f'(mid {_f(mx)} {_f(sy * my)}) (end {_f(x2)} {_f(sy * y2)})',
                f'\t\t\t(stroke (width {_f(w)}) (type solid))',
                f'\t\t\t(layer "{kl}")',
                f'\t\t)']
    if t == 'shape':
        x, y = _mm(el.get('x')), sy * _mm(el.get('y'))
        w, h = _mm(el.get('w', '0')), _mm(el.get('h', '0'))
        outline = _mm(el.get('outline', '0'))
        fill = 'none' if outline else 'solid'
        if int(el.get('roundness', 0)) == 100:
            r = w / 2
            return [f'\t\t(fp_circle',
                    f'\t\t\t(center {_f(x)} {_f(y)}) (end {_f(x + r)} {_f(y)})',
                    f'\t\t\t(stroke (width {_f(outline)}) (type solid))',
                    f'\t\t\t(fill {fill})',
                    f'\t\t\t(layer "{kl}")',
                    f'\t\t)']
        return [f'\t\t(fp_rect',
                f'\t\t\t(start {_f(x - w/2)} {_f(y - h/2)}) '
                f'(end {_f(x + w/2)} {_f(y + h/2)})',
                f'\t\t\t(stroke (width {_f(outline)}) (type solid))',
                f'\t\t\t(fill {fill})',
                f'\t\t\t(layer "{kl}")',
                f'\t\t)']
    if t == 'polygon':
        w = _mm(el.get('width', '0'))
        pts = ' '.join(f'(xy {_f(_mm(v.get("x", "0")))} '
                       f'{_f(sy * _mm(v.get("y", "0")))})'
                       for v in el.findall('vertex'))
        return [f'\t\t(fp_poly',
                f'\t\t\t(pts {pts})',
                f'\t\t\t(stroke (width {_f(w)}) (type solid))',
                f'\t\t\t(fill solid)',
                f'\t\t\t(layer "{kl}")',
                f'\t\t)']
    if t == 'text':
        text = (el.text or '').strip()
        if text.startswith('>'):
            return []          # placeholders handled as properties
        x, y = _mm(el.get('x', '0')), _mm(el.get('y', '0'))
        phi = float(el.get('rot', '0') or '0')
        rot = (theta - phi) % 360 if bottom else (theta + phi) % 360
        just = _justify(el.get('align', 'bottom-left'), flip_v=True)
        if (el.get('mirror') == '1') != bottom:
            just = _merge_mirror(just)
        return [f'\t\t(fp_text user {_q(_eagle_overbar_to_kicad(text))}',
                f'\t\t\t(at {_f(x)} {_f(y if bottom else -y)} {_f(rot)})',
                f'\t\t\t(layer "{kl}")',
                f'\t\t\t(effects {_font(el.get("size", "1000"), el.get("ratio"), el.get("width"))}'
                f'{just})',
                f'\t\t)']
    return []


def _merge_mirror(just):
    """Fold 'mirror' into an existing (justify ...) clause, or make one."""
    if not just:
        return ' (justify mirror)'
    return just.replace(')', ' mirror)')


def _kicad_mmh3(data, seed=0xABBA2345):
    """KiCad's HASH_128 checksum string for an embedded file: incremental
    MurmurHash3 x64_128 over the DECOMPRESSED bytes, seed EMBEDDED_FILES::
    Seed() = 0xABBA2345, formatted h1||h2 as %016X (mmh3_hash.h /
    embedded_files.cpp), with the "V1" tail: the last partial block is
    zero-padded to 4-byte alignment and the PADDED length feeds both the
    tail switch and the final mix (mmh3_hash.h addDataV1 — released
    KiCad 10.0 both writes and VERIFIES this variant; a wrong checksum
    makes pcbnew silently reject the whole board, proven by live pcbnew
    bisect). KiCad master fixed the tail (canonical MurmurHash3) but
    keeps V1 as an accepted legacy fallback on load — so V1 is the one
    encoding valid for BOTH. Validated against all 23 embedded models in
    testData/video/video.kicad_pcb + live pcbnew 10.0 load probes."""
    M = (1 << 64) - 1
    c1, c2 = 0x87c37b91114253d5, 0x4cf5ad432745937f

    def rotl(x, r):
        return ((x << r) | (x >> (64 - r))) & M

    def fmix(k):
        k ^= k >> 33
        k = k * 0xff51afd7ed558ccd & M
        k ^= k >> 33
        k = k * 0xc4ceb9fe1a85ec53 & M
        return k ^ (k >> 33)

    h1 = h2 = seed
    n = len(data) // 16
    for (k1, k2) in struct.iter_unpack('<QQ', data[:n * 16]):
        k1 = rotl(k1 * c1 & M, 31) * c2 & M
        h1 ^= k1
        h1 = (rotl(h1, 27) + h2) & M
        h1 = (h1 * 5 + 0x52dce729) & M
        k2 = rotl(k2 * c2 & M, 33) * c1 & M
        h2 ^= k2
        h2 = (rotl(h2, 31) + h1) & M
        h2 = (h2 * 5 + 0x38495ab5) & M
    tail = data[n * 16:]
    total = n * 16
    if tail:
        pad = 4 - (len(tail) + 4) % 4
        tail = tail + b'\0' * pad
        total += len(tail)
    tl = total & 15
    tb = tail[:tl]
    k1 = k2 = 0
    for i in range(len(tb) - 1, 7, -1):
        k2 |= tb[i] << (8 * (i - 8))
    if tl >= 9:
        k2 = rotl(k2 * c2 & M, 33) * c1 & M
        h2 ^= k2
    for i in range(min(len(tb), 8) - 1, -1, -1):
        k1 |= tb[i] << (8 * i)
    if tl >= 1:
        k1 = rotl(k1 * c1 & M, 31) * c2 & M
        h1 ^= k1
    h1 ^= total
    h2 ^= total
    h1 = (h1 + h2) & M
    h2 = (h2 + h1) & M
    h1, h2 = fmix(h1), fmix(h2)
    h1 = (h1 + h2) & M
    h2 = (h2 + h1) & M
    return f'{h1:016X}{h2:016X}'


def _emit_embedded_files(out, embedded):
    """(embedded_files ...) board-level block: each model's bytes zstd-
    compressed then base64, wrapped at 76 chars (dialect ground truth:
    testData/video/video.kicad_pcb), checksum over the raw bytes."""
    out.append('\t(embedded_files')
    for name in sorted(embedded):
        data = embedded[name].read_bytes()
        b64 = base64.b64encode(
            zstandard.ZstdCompressor().compress(data)).decode('ascii')
        chunks = [b64[i:i + 76] for i in range(0, len(b64), 76)] or ['']
        out.append('\t\t(file')
        out.append(f'\t\t\t(name {_q(name)})')
        out.append('\t\t\t(type model)')
        out.append(f'\t\t\t(data |{chunks[0]}' if len(chunks) == 1
                   else f'\t\t\t(data |{chunks[0]}\n' +
                        '\n'.join(f'\t\t\t\t{c}' for c in chunks[1:]))
        out[-1] += '|'
        out.append('\t\t\t)')
        out.append(f'\t\t\t(checksum "{_kicad_mmh3(data)}")')
        out.append('\t\t)')
    out.append('\t)')


def _emit_stackup(out, coppers, dielectrics):
    """(stackup ...) inside (setup) from the IR stack formula (plan
    sharded-chasing-ritchie / decisions.md). Copper + dielectric thickness,
    ε/tanδ only when the formula carries them — no invented SI facts (no
    "FR4", no default 4.5/0.02). core/prepreg is the fab's decision, not
    modelled: labels follow KiCad's own default generator (single gap ->
    core, else outer gaps prepreg, inner core); import ignores them.
    Silk/paste/mask boilerplate and copper_finish "None" as KiCad 10 writes
    them (ground truth Pocket-Lab-Bench-Power). Appearance attrs
    (FINISH/MASK_COLOR/SILK_COLOR) deliberately NOT wired (user decision
    2026-07-15: v1 carries thickness/ε/tanδ only)."""
    n = len(coppers)
    names = ['F.Cu'] + [f'In{k}.Cu' for k in range(1, n - 1)] + ['B.Cu']
    out += ['\t\t(stackup',
            '\t\t\t(layer "F.SilkS"\n\t\t\t\t(type "Top Silk Screen")\n\t\t\t)',
            '\t\t\t(layer "F.Paste"\n\t\t\t\t(type "Top Solder Paste")\n\t\t\t)',
            '\t\t\t(layer "F.Mask"\n\t\t\t\t(type "Top Solder Mask")'
            '\n\t\t\t\t(thickness 0.01)\n\t\t\t)']
    for i, cu in enumerate(coppers):
        out += [f'\t\t\t(layer "{names[i]}"',
                f'\t\t\t\t(type "copper")',
                f'\t\t\t\t(thickness {_f(cu / 1000)})',
                f'\t\t\t)']
        if i < len(dielectrics):
            d_um, eps, tand = dielectrics[i]
            kind = 'core' if len(dielectrics) == 1 else \
                   ('prepreg' if i in (0, len(dielectrics) - 1) else 'core')
            out += [f'\t\t\t(layer "dielectric {i + 1}"',
                    f'\t\t\t\t(type "{kind}")',
                    f'\t\t\t\t(thickness {_f(d_um / 1000)})']
            if eps is not None:
                out.append(f'\t\t\t\t(epsilon_r {_f(eps)})')
            if tand is not None:
                out.append(f'\t\t\t\t(loss_tangent {_f(tand)})')
            out.append('\t\t\t)')
    out += ['\t\t\t(layer "B.Mask"\n\t\t\t\t(type "Bottom Solder Mask")'
            '\n\t\t\t\t(thickness 0.01)\n\t\t\t)',
            '\t\t\t(layer "B.Paste"\n\t\t\t\t(type "Bottom Solder Paste")\n\t\t\t)',
            '\t\t\t(layer "B.SilkS"\n\t\t\t\t(type "Bottom Silk Screen")\n\t\t\t)',
            '\t\t\t(copper_finish "None")',
            '\t\t\t(dielectric_constraints no)',
            '\t\t)']


def _placeholder(fp_el, name):
    for t in fp_el.findall('text'):
        if (t.text or '').strip() == '>' + name:
            return t
    return None


def _emit_footprint(out, e, fp, lib_name, value, frame, pad_nets, n_copper,
                    sym_path=None, net_code=None, refdes=None,
                    net_display=None, model_name=None):
    des = e.get('name')
    # des is the IR element ADDRESS (module parts: INST:REFDES colon form) —
    # used for uuid/pad-net keys. The VISIBLE reference must match the
    # schematic symbol (offset-flattened, e.g. TM8:R21 -> R821), so a caller
    # passes the KiCad refdes explicitly; falls back to des for top parts.
    refdes = refdes or des
    bottom = e.get('side') == 'bottom'
    theta = float(e.get('rot', '0') or '0')
    at_rot = (theta + 180) % 360 if bottom else theta % 360

    def log(what, detail=''):
        import_log.log('kicad_pcb', des, what, detail)

    if e.findall('text'):
        log('SMASHED_TEXT deferred', 'element-level placeholder overrides '
            'not carried to KiCad board yet')

    # nickname:name must match the schematic Footprint property AND the
    # .pretty file — the project puts every footprint in ONE library under a
    # sanitized name (kicad_project_exporter), so a project export overrides
    # the per-part Eagle library with the project nickname
    fp_id = f'{lib_name}:{sanitize_filename(fp.get("name", "?"))}'
    out.append(f'\t(footprint {_q(fp_id)}')
    out.append(f'\t\t(layer "{"B.Cu" if bottom else "F.Cu"}")')
    out.append(f'\t\t(uuid "{_uuid("fp", des)}")')
    out.append(f'\t\t(at {_f(frame.x(e.get("x")))} {_f(frame.y(e.get("y")))}'
               f' {_f(at_rot)})')
    # link to the schematic symbol (makes "Update PCB from Schematic" a
    # no-op); absent for board-only parts and module-instance parts (logged)
    if sym_path:
        out.append(f'\t\t(path "{sym_path}")')
    elif ':' not in des:
        log('SYMBOL_LINK missing', 'no schematic path (footprint unlinked '
            'until F8 re-associates by refdes)')

    # Reference/Value properties anchored at the footprint's own >NAME/>VALUE
    # placeholders (their local frame), value hidden when placeholder absent
    for prop, text in (('Reference', refdes), ('Value', value)):
        ph = _placeholder(fp, 'NAME' if prop == 'Reference' else 'VALUE')
        if ph is not None:
            px, py = _mm(ph.get('x', '0')), _mm(ph.get('y', '0'))
            phi = float(ph.get('rot', '0') or '0')
            layer = _fp_layer_name(ph.get('layer') or '125', bottom) or 'F.SilkS'
            size, ratio, hide = ph.get('size', '1000'), ph.get('ratio'), False
        else:
            px, py, phi = 0, 0, 0
            layer = 'B.Fab' if bottom else 'F.Fab'
            size, ratio, hide = '1000', None, True
        rot = (theta - phi) % 360 if bottom else (theta + phi) % 360
        just = _justify((ph.get('align', 'bottom-left') if ph is not None
                         else 'bottom-left'), flip_v=True)
        if bottom:
            just = _merge_mirror(just)
        shown = text if prop == 'Reference' else _eagle_overbar_to_kicad(text)
        out.append(f'\t\t(property {_q(prop)} {_q(shown)}')
        out.append(f'\t\t\t(at {_f(px)} {_f(py if bottom else -py)} {_f(rot)})')
        out.append(f'\t\t\t(layer "{layer}")')
        if hide:
            out.append(f'\t\t\t(hide yes)')
        out.append(f'\t\t\t(effects {_font(size, ratio, ph.get("width") if ph is not None else None)}{just})')
        out.append(f'\t\t)')

    anti_children = []
    for child in fp:
        if child.tag in ('description', 'model3d', 'pin-mapping', 'attributes'):
            continue
        if (child.get('layer') or '').startswith('!'):
            # anti-copper (fiducial restrict ring, connector keepout) ->
            # board keepout, emitted below after the footprint closes
            anti_children.append(child)
            continue
        if child.tag in ('smd', 'pad', 'hole'):
            phi = float(child.get('rot', '0') or '0')
            # bottom +180: pcbnew's own flip writes pad angle θ−φ+180 (its
            # trapezoid ground truth: fp 30° flipped -> pad 285, not 105);
            # texts do NOT get the +180 (same ground truth: 135)
            abs_rot = (theta - phi + 180) if bottom else (theta + phi)
            net = pad_nets.get((des, child.get('name')))
            # pad carries BOTH code and name: (net <code> "<display name>")
            code = (net_code or {}).get(net, 0)
            disp = (net_display or {}).get(net, net)
            net_s = f'{code} {_q(disp)}' if net else None
            out.extend(_pad_lines(child, bottom, net_s, abs_rot, log))
        else:
            out.extend(_fp_geometry_lines(child, bottom, theta, log))

    m3 = fp.find('model3d')
    if m3 is not None and model_name:
        # Shared transform math (kicad_exporter.model3d_kicad_xyz — Euler
        # order re-decomposition, offsets pass through). The block lives in
        # the footprint's LOCAL frame — KiCad applies placement and the
        # back-side flip itself.
        tx, ty, tz, rx, ry, rz = (_f(v) for v in model3d_kicad_xyz(m3))
        out += [f'\t\t(model "kicad-embed://{model_name}"',
                f'\t\t\t(offset',
                f'\t\t\t\t(xyz {tx} {ty} {tz})',
                f'\t\t\t)',
                f'\t\t\t(scale',
                f'\t\t\t\t(xyz 1 1 1)',
                f'\t\t\t)',
                f'\t\t\t(rotate',
                f'\t\t\t\t(xyz {rx} {ry} {rz})',
                f'\t\t\t)',
                f'\t\t)']
    out.append('\t)')

    # Footprint anti-copper (ir_schema.md "АНТИ-слои") -> board-level keepout
    # zones, PLACED to board coords via place_ir_element (the very placement
    # math the verifier's oracle uses, so the keepout lands exactly over the
    # placed part). A footprint-embedded zone would follow the part in the
    # KiCad editor, but Babel is an EXIT path (edited in Eagle/swift, not
    # KiCad) — a placed board keepout is the simpler correct choice.
    ex, ey = float(e.get('x')), float(e.get('y'))
    for i, child in enumerate(anti_children):
        _emit_keepout(out, place_ir_element(child, ex, ey, theta, bottom),
                      frame, f'{des}:{i}')


def _stroke_geo(tag, el, frame, log_ctx, net=None, width_key='width'):
    """Free/board geometry -> gr_*/segment/arc lines. Returns [] + logs when
    the layer has no KiCad projection."""
    kl = _board_layer_name(el.get('layer'), log_ctx)
    if kl is None:
        import_log.log('kicad_pcb', log_ctx, 'GEOMETRY dropped',
                       f'{tag} layer={el.get("layer")} (no KiCad projection)')
        return []
    is_track = net is not None
    w = _f(_mm(el.get(width_key, '120')))
    # a track/arc references its net by CODE only (no name field), unlike a pad
    net_s = f'\n\t\t(net {net})' if is_track else ''
    if tag == 'line':
        x1, y1 = _f(frame.x(el.get('x1'))), _f(frame.y(el.get('y1')))
        x2, y2 = _f(frame.x(el.get('x2'))), _f(frame.y(el.get('y2')))
        if is_track:
            return [f'\t(segment\n\t\t(start {x1} {y1})\n\t\t(end {x2} {y2})'
                    f'\n\t\t(width {w})\n\t\t(layer "{kl}"){net_s}'
                    f'\n\t\t(uuid "{_uuid("seg", log_ctx, x1, y1, x2, y2, kl)}")\n\t)']
        return [f'\t(gr_line\n\t\t(start {x1} {y1})\n\t\t(end {x2} {y2})'
                f'\n\t\t(stroke (width {w}) (type solid))\n\t\t(layer "{kl}")'
                f'\n\t\t(uuid "{_uuid("grl", log_ctx, x1, y1, x2, y2, kl)}")\n\t)']
    if tag == 'arc':
        x1, y1 = float(el.get('x1')), float(el.get('y1'))
        x2, y2 = float(el.get('x2')), float(el.get('y2'))
        mx, my = arc_mid(x1, y1, x2, y2, float(el.get('curve', '0')))
        s = f'(start {_f(frame.x(x1))} {_f(frame.y(y1))})'
        m = f'(mid {_f(frame.x(mx))} {_f(frame.y(my))})'
        e_ = f'(end {_f(frame.x(x2))} {_f(frame.y(y2))})'
        if is_track:
            return [f'\t(arc\n\t\t{s}\n\t\t{m}\n\t\t{e_}'
                    f'\n\t\t(width {w})\n\t\t(layer "{kl}"){net_s}'
                    f'\n\t\t(uuid "{_uuid("trkarc", log_ctx, x1, y1, x2, y2)}")\n\t)']
        return [f'\t(gr_arc\n\t\t{s}\n\t\t{m}\n\t\t{e_}'
                f'\n\t\t(stroke (width {w}) (type solid))\n\t\t(layer "{kl}")'
                f'\n\t\t(uuid "{_uuid("grarc", log_ctx, x1, y1, x2, y2)}")\n\t)']
    return []


def _zone_pts(edges, frame):
    """Offset edges (IR µm, ('line'/'arc', x1, y1, x2, y2, curve)) -> KiCad
    zone (pts ...) lines. Node semantics ground truth pcbnew: the pts list
    alternates (xy) points and (arc start mid end) nodes; line edges are
    implied between consecutive nodes, an arc's start absorbs the preceding
    point, closure back to the first node is implicit."""
    lines = ['\t\t\t(pts']
    prev_arc = False
    for i, (kind, x1, y1, x2, y2, curve) in enumerate(edges):
        if kind == 'arc' and curve:
            mx, my = arc_mid(x1, y1, x2, y2, curve)
            lines += [f'\t\t\t\t(arc',
                      f'\t\t\t\t\t(start {_f(frame.x(x1))} {_f(frame.y(y1))})',
                      f'\t\t\t\t\t(mid {_f(frame.x(mx))} {_f(frame.y(my))})',
                      f'\t\t\t\t\t(end {_f(frame.x(x2))} {_f(frame.y(y2))})',
                      f'\t\t\t\t)']
            prev_arc = True
        else:
            if i == 0 or not prev_arc:
                lines.append(f'\t\t\t\t(xy {_f(frame.x(x1))} {_f(frame.y(y1))})')
            prev_arc = False
    lines.append('\t\t\t)')
    return lines


def _emit_zone(out, poly, net_name, frame, uid_key, default_clearance_um,
               net_code=None, net_display=None):
    """IR pour <polygon> (pen-centerline contour, decisions.md "МОДЕЛЬ
    ПЕРА") -> KiCad zone: outline = contour offset OUTWARD by width/2
    (KiCad's outline is a hard copper boundary). Offset failure = the
    contour degenerates under this pen -> hard reject with context.

    Thermal relief carries NO stored geometry (decisions.md 2026-07-12):
    the relief gap IS the zone's clearance, and the spoke width IS the pen
    width (the floor — a spoke thinner than one pen stroke can't exist).
    """
    kl = _board_layer_name(poly.get('layer'), net_name)
    if kl is None:
        import_log.log('kicad_pcb', net_name, 'POUR dropped',
                       f'layer={poly.get("layer")} has no KiCad projection')
        return
    w = float(poly.get('width', '0') or '0')
    vs = [(float(v.get('x')), float(v.get('y')),
           float(v.get('curve', 0) or 0)) for v in poly.findall('vertex')]
    try:
        edges = offset_contour(vs, w / 2)
    except ValueError as e:
        raise ValueError(f'pour {net_name!r} layer {poly.get("layer")}: {e}')
    rank = int(poly.get('rank', '1'))
    fill_pct = max(1, min(100, int(poly.get('fill', '100'))))
    solid = poly.get('thermals') == '0'
    # effective zone clearance = max(explicit isolate, DRC min): the
    # manufacturer's min clearance is a hard FLOOR — an isolate below it is
    # unmanufacturable, so it can only ADD to the minimum, never undercut it
    isolate_um = float(poly.get('clearance')) if poly.get('clearance') else 0.0
    gap_um = max(isolate_um, default_clearance_um)
    spoke_um = w                               # pen width = the spoke floor

    out.append(f'\t(zone')
    # zone splits the fact across TWO fields: net-code + separate net_name
    out.append(f'\t\t(net {(net_code or {}).get(net_name, 0)})')
    out.append(f'\t\t(net_name {_q((net_display or {}).get(net_name, net_name))})')
    out.append(f'\t\t(layer "{kl}")')
    out.append(f'\t\t(uuid "{_uuid("zone", uid_key)}")')
    out.append(f'\t\t(hatch edge 0.5)')
    out.append(f'\t\t(priority {7 - rank})')
    # connect_pads: 'yes' forces solid, its absence = thermal (KiCad
    # default mode). ALWAYS carry the zone clearance = the resolved gap
    # (explicit isolate, else the board-rule min) — otherwise KiCad falls
    # back to its own 0.5 mm zone default instead of the DRC minimum, same
    # rule as Eagle's isolate=0 (decisions.md 2026-07-12: gap ≡ clearance).
    out.append(f'\t\t(connect_pads' + (' yes' if solid else ''))
    out.append(f'\t\t\t(clearance {_f(gap_um / 1000)})')
    out.append(f'\t\t)')
    out.append(f'\t\t(min_thickness {_f(w / 1000)})')
    out.append(f'\t\t(fill yes')
    if fill_pct < 100:
        # IR fill% = thickness/(thickness+gap): pen-wide strokes, gap from %
        hgap = w * (100 - fill_pct) / fill_pct
        out.append(f'\t\t\t(mode hatch)')
        out.append(f'\t\t\t(hatch_thickness {_f(w / 1000)})')
        out.append(f'\t\t\t(hatch_gap {_f(hgap / 1000)})')
        out.append(f'\t\t\t(hatch_orientation 0)')
    # gap = clearance, spoke = pen width (derived, never stored). Emitted
    # for solid zones too (inert there, but keeps KiCad's 0.5 mm default
    # from showing on an unused field)
    out.append(f'\t\t\t(thermal_gap {_f(gap_um / 1000)})')
    out.append(f'\t\t\t(thermal_bridge_width {_f(spoke_um / 1000)})')
    out.append(f'\t\t)')
    out.append(f'\t\t(polygon')
    out.extend(_zone_pts(edges, frame))
    out.append(f'\t\t)')
    out.append(f'\t)')


def _emit_keepout(out, el, frame, idx):
    """Anti-copper free geometry (!N polygon/shape) -> KiCad rule-area
    forbidding copper pour on that layer (ir_schema.md "АНТИ-слои": an anti
    object subtracts its FILLED region; KiCad does the subtraction at fill
    time). Non-fillable anti primitives (line/arc/text) stay deferred."""
    anti, n = parse_layer(el.get('layer'))
    kl = _copper_name(n, None) if is_copper(n) else None
    if kl is None:
        import_log.log('kicad_pcb', 'plain', 'ANTI dropped',
                       f'{el.tag} layer={el.get("layer")} (non-copper anti '
                       f'has no KiCad projection)')
        return
    if el.tag == 'polygon':
        edges = []
        vs = [(float(v.get('x')), float(v.get('y')),
               float(v.get('curve', 0) or 0)) for v in el.findall('vertex')]
        m = len(vs)
        for i, (x1, y1, c) in enumerate(vs):
            x2, y2, _ = vs[(i + 1) % m]
            edges.append(('arc' if c else 'line', x1, y1, x2, y2, c))
    elif el.tag == 'shape':
        x, y = float(el.get('x')), float(el.get('y'))
        w, h = float(el.get('w', '0')), float(el.get('h', '0'))
        if int(el.get('roundness', 0)) == 100:
            r = w / 2
            edges = [('arc', x - r, y, x + r, y, 180.0),
                     ('arc', x + r, y, x - r, y, 180.0)]
        else:
            a = math.radians(float(el.get('rot', '0') or '0'))
            cs, sn = math.cos(a), math.sin(a)
            corners = [(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)]
            p = [(x + dx * cs - dy * sn, y + dx * sn + dy * cs)
                 for dx, dy in corners]
            edges = [('line', *p[i], *p[(i + 1) % 4], 0) for i in range(4)]
    else:
        import_log.log('kicad_pcb', 'plain', 'ANTI deferred',
                       f'{el.tag} layer={el.get("layer")} (stroked anti '
                       f'primitive -> keepout not expressible yet)')
        return
    out += [f'\t(zone',
            f'\t\t(net 0)',
            f'\t\t(net_name "")',
            f'\t\t(layer "{kl}")',
            f'\t\t(uuid "{_uuid("keepout", idx)}")',
            f'\t\t(hatch edge 0.5)',
            f'\t\t(keepout',
            f'\t\t\t(tracks allowed)',
            f'\t\t\t(vias allowed)',
            f'\t\t\t(pads allowed)',
            f'\t\t\t(copperpour not_allowed)',
            f'\t\t\t(footprints allowed)',
            f'\t\t)',
            f'\t\t(min_thickness 0.25)',
            f'\t\t(fill',
            f'\t\t)',
            f'\t\t(polygon']
    out.extend(_zone_pts(edges, frame))
    out += [f'\t\t)', f'\t)']


def export_board_kicad(ir_path, output_path, layout_name=None, sym_paths=None,
                       lib_nickname=None, minst_page_name=None):
    """IR project -> one .kicad_pcb for the named (or single) <layout>.

    sym_paths: {designator -> '/uuid' schematic path} so each footprint
    carries the (path ...) link to its schematic symbol (F8 no-op). Absent
    (standalone board export) -> footprints stay unlinked (logged).
    lib_nickname: the project's single footprint-library nickname; when set
    (project export) it overrides each part's Eagle library so the board's
    nickname:name matches the schematic and fp-lib-table. Absent (standalone)
    -> the per-part Eagle library is kept.
    """
    ir_path = Path(ir_path)
    sym_paths = sym_paths or {}
    root = ET.parse(ir_path).getroot()
    layouts = root.findall('layout')
    if not layouts:
        raise ValueError(f'{ir_path}: no <layout> — nothing to export')
    layout = next((l for l in layouts
                   if layout_name in (None, l.get('name'))), None)
    if layout is None:
        raise ValueError(f'{ir_path}: no layout named {layout_name!r}')

    schem = root.find('schematic')
    comp_by_name = {c.get('name'): c for c in root.findall('component')}
    inst_by_des = {}
    if schem is not None:
        for i in schem.findall('instance'):
            inst_by_des.setdefault(i.get('name'), i)
    module_by_name = {m.get('name'): m for m in root.findall('module')}
    local_fp = {f.get('name'): f for f in layout.findall('footprint')}

    # 3D models: sidecar STEP dir next to the IR (ir_schema.md "Соглашение
    # о расположении файлов моделей"); resolved once per footprint, bytes
    # embedded into the board at the end. A <model3d> with no sidecar file
    # is the documented degradation: model lost, logged, board still valid.
    sidecar_dir = ir_path.parent / ir_path.stem
    embedded = {}                 # file name -> Path
    _model_cache = {}             # id(fp) -> name | None
    def _model_for(fp):
        key = id(fp)
        if key not in _model_cache:
            name = None
            if fp.find('model3d') is not None:
                src = resolve_model3d_file(fp, sidecar_dir)
                if src is None:
                    import_log.log('kicad_pcb', fp.get('name'),
                                   'MODEL3D dropped',
                                   f'no STEP file in {sidecar_dir.name}/')
                else:
                    name = src.name
                    embedded[name] = src
            _model_cache[key] = name
        return _model_cache[key]

    def _kicad_refdes(des):
        """IR element address -> the reference KiCad shows, matching the
        schematic symbol. Module part INST:REFDES is flattened by the module
        instance's offset (TM8:R21 @ offset 800 -> R821); top parts unchanged.
        (KiCad-targeted module instances always carry an offset — the schematic
        exporter hard-rejects offset-less ones — so this never emits a colon.)"""
        if ':' not in des:
            return des
        minst_name, part = des.split(':', 1)
        minst = inst_by_des.get(minst_name)
        off = minst.get('offset') if minst is not None else None
        return instance_designator(part, minst_name, off)

    def resolve_instance(des):
        if ':' not in des:
            return inst_by_des.get(des)
        minst_name, part = des.split(':', 1)
        minst = inst_by_des.get(minst_name)
        mod = module_by_name.get(minst.get('module')) if minst is not None else None
        if mod is None:
            return None
        return next((i for i in mod.findall('instance')
                     if i.get('name') == part), None)

    coppers, _diel = parse_stack(layout.get('stack'))
    n_copper = len(coppers)
    _c, dielectrics = parse_stack(layout.get('stack'))
    thickness_mm = (sum(coppers) + sum(d[0] for d in dielectrics)) / 1000

    # --- frame: bbox over everything positional
    xs, ys = [], []
    for el in layout.iter():
        for kx, ky in (('x', 'y'), ('x1', 'y1'), ('x2', 'y2')):
            if el.get(kx) is not None and el.get(ky) is not None:
                try:
                    xs.append(float(el.get(kx))); ys.append(float(el.get(ky)))
                except ValueError:
                    pass
    frame = _Frame(min(xs, default=0), max(ys, default=0))

    # --- pad nets from contactrefs
    pad_nets, net_names = {}, []
    for sig in layout.findall('signal'):
        net_names.append(sig.get('name'))
        for cr in sig.findall('contactref'):
            pad_nets[(cr.get('element'), cr.get('pad'))] = sig.get('name')

    # --- net CODES: KiCad's canonical connectivity is by integer net-code
    # (a name-only ref makes zones/vias DRC-orphan even when the board LOOKS
    # right, because the editor heals the ratsnest by name). Code 0 is the
    # reserved "no net"; every named signal gets a stable 1..N code, declared
    # once in the (net ...) table below and referenced by number everywhere.
    net_code = {'': 0}
    for nm in net_names:
        if nm not in net_code:
            net_code[nm] = len(net_code)

    # --- net NAMES as KiCad's netlist spells them, so board copper and
    # schematic agree (mismatch -> F8 "unknown net" on every via/zone). A
    # module-local net is Eagle's INST:LOCAL colon form -> KiCad's hierarchical
    # path /{page}/{INST}/{LOCAL} (ground truth testData/t2: /PAGE2/MODULE1/9V,
    # never a colon). Top-level nets are global labels in our schematic ->
    # plain name, unchanged. `page` is the top-level SHEET NAME the module
    # instance sits on (from top_level_sheets), passed in per instance.
    minst_page_name = minst_page_name or {}
    def _net_disp(ir_name):
        if ':' not in ir_name:
            return ir_name
        minst, local = ir_name.split(':', 1)
        page = minst_page_name.get(minst)
        return f'/{page}/{minst}/{local}' if page else ir_name
    net_display = {nm: _net_disp(nm) for nm in net_code}

    # thermal gap ≡ zone clearance (decisions.md 2026-07-12): a pour with no
    # explicit isolate uses the board-rule clearance as its relief gap
    rules_el = layout.find('rules')
    default_clearance_um = float(rules_el.get('clearance')) \
        if rules_el is not None and rules_el.get('clearance') else 200.0

    out = [f'(kicad_pcb',
           f'\t(version {_VERSION})',
           f'\t(generator "babel")',
           f'\t(generator_version "10.0")',
           f'\t(general',
           f'\t\t(thickness {_f(thickness_mm)})',
           f'\t\t(legacy_teardrops no)',
           f'\t)',
           f'\t(paper "A4")']

    # --- layers table
    out.append('\t(layers')
    out.append('\t\t(0 "F.Cu" signal)')
    for k in range(2, n_copper):
        out.append(f'\t\t({2 * k} "In{k - 1}.Cu" signal)')
    out.append('\t\t(2 "B.Cu" signal)')
    for lid, name, kind, alias in _STD_LAYERS:
        alias_s = f' "{alias}"' if alias else ''
        out.append(f'\t\t({lid} "{name}" {kind}{alias_s})')
    out.append('\t)')
    out.append('\t(setup')
    # stackup first in setup (node order ground truth: Pocket-Lab v10)
    _emit_stackup(out, coppers, dielectrics)
    # The page-frame offset parks the board in positive page coords; the IR
    # origin itself is marked with KiCad's own user-origin concept so a
    # future KiCad->IR import restores the source coordinate system exactly
    # (rule: aux_axis_origin, when present, IS IR (0,0)) — Eagle boards keep
    # their center-origin through the full round-trip, nothing drifts.
    out.append(f'\t\t(aux_axis_origin {_f(frame.x(0))} {_f(frame.y(0))})')
    out.append(f'\t\t(grid_origin {_f(frame.x(0))} {_f(frame.y(0))})')
    out.append('\t\t(pad_to_mask_clearance 0)')
    out.append('\t\t(allow_soldermask_bridges_in_footprints no)')
    out.append('\t)')

    # --- net table (code -> name), declared before any copper references it
    for nm, code in sorted(net_code.items(), key=lambda kv: kv[1]):
        out.append(f'\t(net {code} {_q(net_display[nm])})')

    # --- elements
    for e in layout.findall('element'):
        des = e.get('name')
        if e.get('footprint'):        # board-only padless object
            fp = local_fp.get(e.get('footprint'))
            lib, value = fp.get('library', 'babel') if fp is not None else 'babel', ''
            if fp is None:
                import_log.log('kicad_pcb', des, 'ELEMENT dropped',
                               f'local footprint {e.get("footprint")!r} missing')
                continue
            _emit_footprint(out, e, fp, lib_nickname or lib, value, frame,
                            pad_nets, n_copper, net_code=net_code,
                            net_display=net_display, model_name=_model_for(fp))
            continue
        inst = resolve_instance(des)
        comp = comp_by_name.get(inst.get('component')) if inst is not None else None
        fp = _instance_footprint(comp, inst) if comp is not None else None
        if fp is None:
            import_log.log('kicad_pcb', des, 'ELEMENT dropped',
                           'no schematic instance/footprint resolved')
            continue
        value = inst.get('value') or comp.get('name') or ''
        _emit_footprint(out, e, fp, lib_nickname or comp.get('library', 'babel'),
                        value, frame, pad_nets, n_copper,
                        sym_path=sym_paths.get(des), net_code=net_code,
                        refdes=_kicad_refdes(des), net_display=net_display,
                        model_name=_model_for(fp))

    # --- layout-level mounting holes: KiCad has no bare-board NPTH
    # primitive — each becomes a one-pad synthetic footprint
    for i, h in enumerate(layout.findall('hole')):
        d = _mm(h.get('drill'))
        out += [f'\t(footprint "babel:HOLE_{_f(d)}mm"',
                f'\t\t(layer "F.Cu")',
                f'\t\t(uuid "{_uuid("hole", i)}")',
                f'\t\t(at {_f(frame.x(h.get("x")))} {_f(frame.y(h.get("y")))})',
                f'\t\t(property "Reference" "H{i + 1}"',
                f'\t\t\t(at 0 0 0)', f'\t\t\t(layer "F.Fab")',
                f'\t\t\t(hide yes)',
                f'\t\t\t(effects {_font("1000", None)})', f'\t\t)',
                f'\t\t(property "Value" ""',
                f'\t\t\t(at 0 0 0)', f'\t\t\t(layer "F.Fab")',
                f'\t\t\t(hide yes)',
                f'\t\t\t(effects {_font("1000", None)})', f'\t\t)',
                f'\t\t(attr exclude_from_pos_files exclude_from_bom)',
                f'\t\t(pad "" np_thru_hole circle',
                f'\t\t\t(at 0 0)',
                f'\t\t\t(size {_f(d)} {_f(d)})',
                f'\t\t\t(drill {_f(d)})',
                f'\t\t\t(layers "*.Cu" "*.Mask")',
                f'\t\t)', f'\t)']

    # --- free board geometry
    n_keepout = 0
    for el in layout:
        t = el.tag
        if t in ('line', 'arc', 'shape', 'polygon', 'text') and \
                (el.get('layer') or '').startswith('!'):
            n_keepout += 1
            _emit_keepout(out, el, frame, n_keepout)
            continue
        if t in ('line', 'arc'):
            out.extend(_stroke_geo(t, el, frame, 'plain'))
        elif t == 'shape':
            kl = _board_layer_name(el.get('layer'), 'plain')
            if kl is None:
                import_log.log('kicad_pcb', 'plain', 'GEOMETRY dropped',
                               f'shape layer={el.get("layer")}')
                continue
            x, y = float(el.get('x')), float(el.get('y'))
            w, h = _mm(el.get('w', '0')), _mm(el.get('h', '0'))
            outline = _mm(el.get('outline', '0'))
            fill = 'none' if outline else 'solid'
            if int(el.get('roundness', 0)) == 100:
                r = w / 2
                out.append(
                    f'\t(gr_circle\n\t\t(center {_f(frame.x(x))} {_f(frame.y(y))})'
                    f'\n\t\t(end {_f(frame.x(x) + r)} {_f(frame.y(y))})'
                    f'\n\t\t(stroke (width {_f(outline)}) (type solid))'
                    f'\n\t\t(fill {fill})\n\t\t(layer "{kl}")'
                    f'\n\t\t(uuid "{_uuid("grc", x, y)}")\n\t)')
            else:
                wq, hq = w / 2 * 1000, h / 2 * 1000
                out.append(
                    f'\t(gr_rect\n\t\t(start {_f(frame.x(x - wq))} {_f(frame.y(y - hq))})'
                    f'\n\t\t(end {_f(frame.x(x + wq))} {_f(frame.y(y + hq))})'
                    f'\n\t\t(stroke (width {_f(outline)}) (type solid))'
                    f'\n\t\t(fill {fill})\n\t\t(layer "{kl}")'
                    f'\n\t\t(uuid "{_uuid("grr", x, y)}")\n\t)')
        elif t == 'polygon':
            kl = _board_layer_name(el.get('layer'), 'plain')
            if kl is None:
                import_log.log('kicad_pcb', 'plain', 'GEOMETRY dropped',
                               f'polygon layer={el.get("layer")}')
                continue
            w = _f(_mm(el.get('width', '0')))
            fill = 'none' if el.get('fill', '100') == '0' else 'solid'
            pts = ' '.join(f'(xy {_f(frame.x(v.get("x")))} '
                           f'{_f(frame.y(v.get("y")))})'
                           for v in el.findall('vertex'))
            out.append(f'\t(gr_poly\n\t\t(pts {pts})'
                       f'\n\t\t(stroke (width {w}) (type solid))'
                       f'\n\t\t(fill {fill})\n\t\t(layer "{kl}")'
                       f'\n\t\t(uuid "{_uuid("grp", pts[:40])}")\n\t)')
        elif t == 'text':
            kl = _board_layer_name(el.get('layer'), 'plain')
            if kl is None:
                import_log.log('kicad_pcb', 'plain', 'GEOMETRY dropped',
                               f'text layer={el.get("layer")}')
                continue
            text = (el.text or '').strip()
            rot = float(el.get('rot', '0') or '0') % 360
            just = _justify(el.get('align', 'bottom-left'), flip_v=True)
            if el.get('mirror') == '1':
                just = _merge_mirror(just)
            out.append(f'\t(gr_text {_q(_eagle_overbar_to_kicad(text))}'
                       f'\n\t\t(at {_f(frame.x(el.get("x")))} '
                       f'{_f(frame.y(el.get("y")))} {_f(rot)})'
                       f'\n\t\t(layer "{kl}")'
                       f'\n\t\t(effects {_font(el.get("size", "1000"), el.get("ratio"), el.get("width"))}{just})'
                       f'\n\t\t(uuid "{_uuid("grt", text, el.get("x"), el.get("y"))}")\n\t)')

    # --- copper: tracks, vias, pours
    zone_seq = 0
    for sig in layout.findall('signal'):
        name = sig.get('name')
        for c in sig:
            if c.tag in ('line', 'arc'):
                out.extend(_stroke_geo(c.tag, c, frame, name,
                                       net=net_code[name]))
            elif c.tag == 'via':
                if c.get('diameter'):
                    d = _mm(c.get('diameter'))
                else:
                    d = _mm(c.get('drill')) * 2
                    import_log.log('kicad_pcb', name, 'VIA_DIAMETER missing',
                                   f'synthesized {d:g}mm (2x drill)')
                out.append(
                    f'\t(via\n\t\t(at {_f(frame.x(c.get("x")))} '
                    f'{_f(frame.y(c.get("y")))})'
                    f'\n\t\t(size {_f(d)})\n\t\t(drill {_f(_mm(c.get("drill")))})'
                    f'\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net {net_code[name]})'
                    f'\n\t\t(uuid "{_uuid("via", name, c.get("x"), c.get("y"))}")\n\t)')
            elif c.tag == 'polygon':
                zone_seq += 1
                _emit_zone(out, c, name, frame, f'{name}:{zone_seq}',
                           default_clearance_um, net_code=net_code,
                           net_display=net_display)

    out.append('\t(embedded_fonts no)')
    if embedded:
        _emit_embedded_files(out, embedded)
    out.append(')')
    output_path = Path(output_path)
    output_path.write_text('\n'.join(out) + '\n', encoding='utf-8')
    n_el = len(layout.findall('element'))
    print(f'Written: {output_path}  ({n_el} footprints, '
          f'{len(net_names)} nets, {n_copper} copper layers, '
          f'{len(embedded)} embedded 3D models)')
    return output_path
