"""Read the exported .PcbDoc back and check it against the IR it came from.

The strong check is the PADS, not the components: every pad's absolute board
position is compared with the same pad baked from the IR footprint by
ir_util.place_footprint — the IR's own placement math. A wrong rotation, a
wrong mirror axis or a wrong origin all move pads, so this catches the whole
class of placement bugs numerically, without opening Altium.

Prints counters and mismatches only — never file contents.
"""
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from altium_monkey import AltiumPcbLib
from altium_monkey.altium_pcbdoc import AltiumPcbDoc
from altium_monkey.altium_schdoc import AltiumSchDoc

from babel.altium_board_exporter import _chain, _outline_bbox, _outline_segments
from babel import altium_layers
from babel.altium_exporter import (DRC_RULES, _pcb_layer, shape_primitives,
                                   target_layer)
from babel.altium_exporter import channel_designator
from babel.ir_util import (LAYER_DIMENSION, chain_loops, designator_resolver,
                           flatten_loop,
                           instance_footprint, parse_stack, place_footprint,
                           place_ir_element)

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else 'outputs/altium_sch_export')
NAME = sys.argv[2] if len(sys.argv) > 2 else 'tolmach'

TOL_MILS = 0.05          # 1.3 µm — round-trip noise of the mil conversion
fails = []


def check(ok, msg):
    print(('  ok   ' if ok else '  FAIL ') + msg)
    if not ok:
        fails.append(msg)


# The IR is a separate result of the run, next to the project folder
# (`ir_<name>/`); older runs left it inside, or beside the project files.
_ir_path = next((p for p in (OUT.parent / f'ir_{NAME}' / f'{NAME}.swprj',
                             OUT / 'ir' / f'{NAME}.swprj',
                             OUT / f'{NAME}.swprj') if p.exists()),
                OUT / f'{NAME}.swprj')
ir = ET.parse(_ir_path).getroot()
layout = ir.find('layout')
pcb = AltiumPcbDoc.from_file(str(OUT / f'{NAME}.PcbDoc'))
ox, oy = pcb.board.origin_x, pcb.board.origin_y

layer_plan, _pairs = altium_layers.plan(altium_layers.layers_used(ir))
comp_by_name = {c.get('name'): c for c in ir.findall('component')}
# An element addresses a top-level instance or a part inside a module
# instance ('INST:REFDES'); the flattened designator is the one Altium got.
resolve_element = designator_resolver(ir)
local_fp = {f.get('name'): f for f in layout.findall('footprint')}
inst_of, fp_of = {}, {}
for _e in layout.findall('element'):
    _addr = _e.get('name')
    _inst, _des = resolve_element(_addr)
    if ':' in _addr:
        # a channel part is named by the project's channel format, not by the
        # IR's Eagle-style flattening (altium_exporter.channel_designator)
        _room, _canon = _addr.split(':', 1)
        _des = channel_designator(_room, _canon)
    inst_of[_addr] = (_inst, _des)
    # a board-only padless element (Eagle logo) has no schematic part: its
    # footprint hangs on the layout itself
    fp_of[_e.get('name')] = (local_fp[_e.get('footprint')] if _inst is None
                             else instance_footprint(
                                 comp_by_name[_inst.get('component')], _inst))

# --- outline ---------------------------------------------------------------
loop = _chain(_outline_segments(layout))
min_x, min_y, max_x, max_y = _outline_bbox(loop)
verts = pcb.board.outline.vertices
print('OUTLINE')
check(len(verts) == len(loop), f'{len(verts)} vertices, IR loop has {len(loop)}')
check(sum(1 for v in verts if v.is_arc) == sum(1 for s in loop if s[4]),
      f'{sum(1 for v in verts if v.is_arc)} arc vertices')
bx = [v.x_mils for v in verts]
by = [v.y_mils for v in verts]
check(abs((max(bx) - min(bx)) - (max_x - min_x) / 25.4) < 1.0
      and abs((max(by) - min(by)) - (max_y - min_y) / 25.4) < 1.0,
      f'bbox {(max(bx) - min(bx)):.1f} x {(max(by) - min(by)):.1f} mils '
      f'= {(max_x - min_x) / 1000:.2f} x {(max_y - min_y) / 1000:.2f} mm')
check(abs(min(bx) - ox) < 1.0 and abs(min(by) - oy) < 1.0,
      f'board origin ({ox:.0f}, {oy:.0f}) mils sits on the outline corner')

# --- stack -----------------------------------------------------------------
coppers, dielectrics = parse_stack(layout.get('stack'))
# Only the span BETWEEN the outer copper layers is the IR stack — the entries
# outside it are the solder masks (same slice the importer takes).
v9 = list(pcb.board.v9_stack or [])
cu_idx = [i for i, l in enumerate(v9) if l.is_copper]
span = v9[cu_idx[0]:cu_idx[-1] + 1] if cu_idx else []
cu = [l for l in span if l.is_copper]
di = [l for l in span if l.is_dielectric]
print('STACK')
check(len(cu) == len(coppers), f'{len(cu)} copper layers, IR has {len(coppers)}')
got = [round(l.copper_thickness * 25.4) for l in cu] if cu else []
check(got == coppers, f'copper thickness {got} µm, IR {coppers}')
got_d = [round(l.diel_height * 25.4) for l in di]
check(got_d == [d[0] for d in dielectrics],
      f'dielectric {got_d} µm, IR {[d[0] for d in dielectrics]}')

# --- components ------------------------------------------------------------
els = layout.findall('element')
by_desig = {c.designator: c for c in pcb.components}
print('COMPONENTS')
check(len(pcb.components) == len(els),
      f'{len(pcb.components)} placed, IR has {len(els)} <element>')
missing = [e.get('name') for e in els if inst_of[e.get('name')][1] not in by_desig]
check(not missing, f'every IR element placed (missing: {missing[:5]})')
sides = sum(1 for c in pcb.components if c.layer == 'BOTTOM')
ir_bottom = sum(1 for e in els if e.get('side') == 'bottom')
check(sides == ir_bottom, f'{sides} components on the bottom side, IR {ir_bottom}')

# --- pads: the real placement check ---------------------------------------
pads_by_comp = {}
for p in pcb.pads:
    if p.component_index is not None:
        pads_by_comp.setdefault(p.component_index, []).append(p)
index_of = {c.designator: i for i, c in enumerate(pcb.components)}

worst = 0.0
worst_at = ''
checked = 0
absent = []
for e in els:
    inst, desig = inst_of[e.get('name')]
    fp_el = fp_of[e.get('name')]
    bottom = e.get('side') == 'bottom'
    placed = place_footprint(fp_el, float(e.get('x', 0)), float(e.get('y', 0)),
                             float(e.get('rot', 0) or 0), bottom)
    want = {el.get('name'): (float(el.get('x')), float(el.get('y')))
            for el in placed if el.tag in ('smd', 'pad')}
    got = {p.designator: p for p in pads_by_comp.get(index_of[desig], [])}
    for name, (wx, wy) in want.items():
        p = got.get(name)
        if p is None:
            absent.append(f'{desig}.{name}')
            continue
        dx = abs((p.x_mils - ox) - (wx - min_x) / 25.4)
        dy = abs((p.y_mils - oy) - (wy - min_y) / 25.4)
        checked += 1
        if max(dx, dy) > worst:
            worst, worst_at = max(dx, dy), f'{desig}.{name}'

print('PAD POSITIONS (placement math)')
check(not absent, f'every IR pad present in the PcbDoc (missing: {absent[:5]})')
check(worst <= TOL_MILS,
      f'{checked} pads compared with the IR-baked placement, worst deviation '
      f'{worst:.4f} mils at {worst_at}')

# --- filled rectangles -----------------------------------------------------
# A pad check cannot see this: an Altium Fill is a box PLUS a rotation about
# its own centre, so a rectangle can sit at the right place and still be
# turned the wrong way. Compare what is actually DRAWN — the box after its
# own rotation is applied.
fills_by_comp = {}
for f in pcb.fills:
    if f.component_index is not None:
        fills_by_comp.setdefault(f.component_index, []).append(f)


def _drawn(w, h, rot):
    return (h, w) if round(float(rot or 0)) % 180 == 90 else (w, h)


n_rect = 0
unmapped = 0
rect_bad = []
for e in els:
    inst, desig = inst_of[e.get('name')]
    fp_el = fp_of[e.get('name')]
    bottom = e.get('side') == 'bottom'
    placed = place_footprint(fp_el, float(e.get('x', 0)), float(e.get('y', 0)),
                             float(e.get('rot', 0) or 0), bottom)
    want = []
    for s in placed:
        if s.tag != 'shape' or int(s.get('roundness', 0)) == 100 \
                or float(s.get('outline', 0) or 0) != 0:
            continue
        if _pcb_layer(layer_plan, s.get('layer')) is None:
            unmapped += 1
            continue
        w, h = _drawn(float(s.get('w', 0)) / 25.4, float(s.get('h', 0)) / 25.4,
                      s.get('rot'))
        want.append(((float(s.get('x')) - min_x) / 25.4,
                     (float(s.get('y')) - min_y) / 25.4, w, h))
    got = []
    for f in fills_by_comp.get(index_of[desig], []):
        w = abs(f.pos2_x_mils - f.pos1_x_mils)
        h = abs(f.pos2_y_mils - f.pos1_y_mils)
        w, h = _drawn(w, h, f.rotation)
        got.append(((f.pos1_x_mils + f.pos2_x_mils) / 2 - ox,
                    (f.pos1_y_mils + f.pos2_y_mils) / 2 - oy, w, h))
    n_rect += len(want)
    for a in want:
        if not any(all(abs(a[i] - b[i]) <= TOL_MILS for i in range(4))
                   for b in got):
            rect_bad.append(f'{desig} {a[2]:.1f}x{a[3]:.1f} @({a[0]:.1f},{a[1]:.1f})')

print('FILLED RECTANGLES (as drawn)')
check(not rect_bad, f'{n_rect} rectangles match the IR in centre AND '
                    f'orientation (wrong: {rect_bad[:4]})')
if unmapped:
    print(f'  note  {unmapped} rectangle(s) on IR layers Altium has no home '
          f'for — dropped by the library exporter and logged there')

# --- nothing dropped -------------------------------------------------------
# The mask rectangles of SOD323 vanished because their IR layer had no row in
# the exporter's hardcoded table, and nothing counted them. Count now: every
# footprint drawable must show up as its Altium counterpart.
def _cut_segments(fp_el):
    """The footprint's dimension-layer segments that form closed loops — they
    become ONE board-cutout region each, not tracks."""
    segs, owner = [], {}
    for c in fp_el:
        if c.tag in ('line', 'arc') and c.get('layer') == str(LAYER_DIMENSION):
            s = (float(c.get('x1')), float(c.get('y1')),
                 float(c.get('x2')), float(c.get('y2')),
                 float(c.get('curve', 0) or 0))
            segs.append(s)
            owner.setdefault(s, c)
    loops, _left = chain_loops(segs)
    return loops, sum(len(l) for l in loops)


def _expected(fp_el):
    n = {'track': 0, 'arc': 0, 'fill': 0, 'text': 0}
    loops, n_cut = _cut_segments(fp_el)
    cut_left = n_cut
    for c in fp_el:
        if c.tag not in ('line', 'arc', 'shape', 'text'):
            continue
        # anti-layer geometry counts too: it is emitted as the same primitive
        # on the copper layer, only flagged as a keepout
        if target_layer(layer_plan, c.get('layer'))[0] is None:
            continue
        if c.tag in ('line', 'arc') and c.get('layer') == str(LAYER_DIMENSION) \
                and cut_left:
            cut_left -= 1
            continue
        if c.tag == 'line':
            n['track'] += 1
        elif c.tag == 'arc':
            n['arc'] += 1
        elif c.tag == 'text':
            # Altium gives EVERY component its own .Designator/.Comment
            # strings whether or not the footprint drew a placeholder, so
            # only plain labels can be counted one-for-one here.
            if not (c.text or '').strip().startswith('>'):
                n['text'] += 1
        elif int(c.get('roundness', 0)) == 100:
            n['arc'] += 1                       # circle
        elif float(c.get('outline', 0) or 0) != 0:
            n['track'] += 4                     # outlined rectangle
        else:
            n['fill'] += 1
    return n


got_all = {'track': 0, 'arc': 0, 'fill': 0, 'text': 0}
for prims, kind in ((pcb.tracks, 'track'), (pcb.arcs, 'arc'),
                    (pcb.fills, 'fill'), (pcb.texts, 'text')):
    got_all[kind] = sum(1 for p in prims if p.component_index is not None
                        and not (kind == 'text' and (p.is_designator
                                                     or p.is_comment)))
want_all = {'track': 0, 'arc': 0, 'fill': 0, 'text': 0}
for e in els:
    for k, v in _expected(fp_of[e.get('name')]).items():
        want_all[k] += v
print('FOOTPRINT GRAPHICS (nothing dropped)')
for k in ('track', 'arc', 'fill', 'text'):
    check(got_all[k] == want_all[k],
          f'{got_all[k]} plain {k}s on placed components, IR expects '
          f'{want_all[k]}')
# Visibility is a FLAG on the component (NAMEON), not a missing object: a
# component without a designator primitive makes Altium draw one of its own
# ("Designator1"). So every component keeps its text, and exactly the ones
# whose footprint declares a visible >NAME have it switched on.
des_texts = [t for t in pcb.texts if t.is_designator]
with_des = {t.component_index for t in des_texts}


def _shows_name(e):
    declared = any((t.text or '').strip() == '>NAME'
                   for t in list(fp_of[e.get('name')].findall('text'))
                   + list(e.findall('text')))
    hidden = any((t.text or '').strip() == '>NAME' and t.get('hidden') == 'yes'
                 for t in e.findall('text'))
    return declared and not hidden


want_des = {index_of[inst_of[e.get('name')][1]] for e in els if _shows_name(e)}
shown_des = {i for i, c in enumerate(pcb.components) if c.name_on}
check(with_des >= want_des,
      f'{len(des_texts)} designator string(s) — every component keeps its own '
      f'(missing: {sorted(want_des - with_des)[:4]})')
check(shown_des == want_des,
      f'{len(shown_des)} of them are VISIBLE — exactly the elements whose '
      f'footprint declares a visible >NAME '
      f'(wrong: {sorted(shown_des ^ want_des)[:4]})')

# --- board cutouts a footprint brings with it ------------------------------
# The expected outline is baked from the IR by the IR's own placement math,
# so a wrong rotation or side shows up as a moved cutout, not as a pass.
cut_want = []
for e in els:
    fp_el = fp_of[e.get('name')]
    if not _cut_segments(fp_el)[0]:
        continue
    placed = place_footprint(fp_el, float(e.get('x', 0)), float(e.get('y', 0)),
                             float(e.get('rot', 0) or 0),
                             e.get('side') == 'bottom')
    segs = [(float(c.get('x1')), float(c.get('y1')),
             float(c.get('x2')), float(c.get('y2')),
             float(c.get('curve', 0) or 0))
            for c in placed
            if c.tag in ('line', 'arc') and c.get('layer') == str(LAYER_DIMENSION)]
    for loop in chain_loops(segs)[0]:
        pts = flatten_loop(loop)
        cut_want.append((e.get('name'),
                         (min(p[0] for p in pts) - min_x) / 25.4,
                         (min(p[1] for p in pts) - min_y) / 25.4,
                         (max(p[0] for p in pts) - min(p[0] for p in pts)) / 25.4,
                         (max(p[1] for p in pts) - min(p[1] for p in pts)) / 25.4))

cut_got = [r for r in pcb.regions if r.is_board_cutout]
print('BOARD CUTOUTS (milling contour carried by a footprint)')
check(len(cut_got) == len(cut_want),
      f'{len(cut_got)} board-cutout regions, IR has {len(cut_want)} closed '
      f'dimension-layer loop(s) on placed footprints')
cut_bad = []
for desig, wx, wy, ww, wh in cut_want:
    hit = False
    for r in cut_got:
        xs = [v.x_mils - ox for v in r.outline_vertices]
        ys = [v.y_mils - oy for v in r.outline_vertices]
        if (abs(min(xs) - wx) <= TOL_MILS and abs(min(ys) - wy) <= TOL_MILS
                and abs(max(xs) - min(xs) - ww) <= TOL_MILS
                and abs(max(ys) - min(ys) - wh) <= TOL_MILS):
            hit = True
            break
    if not hit:
        cut_bad.append(f'{desig} {ww:.1f}x{wh:.1f} @({wx:.1f},{wy:.1f})')
check(not cut_bad, f'each sits where the IR places it (wrong: {cut_bad[:3]})')

# --- designator text: the per-instance overrides ---------------------------
# Same discipline as the pads: the expected position is computed by the IR's
# own placement math from the element's <text> child, not read back from what
# the exporter did.
texts_by_comp = {}
for t in pcb.texts:
    if t.component_index is not None:
        texts_by_comp.setdefault(t.component_index, []).append(t)

worst_t = 0.0
worst_t_at = ''
n_ovr = 0
no_text = []
for e in els:
    ov = next((t for t in e.findall('text')
               if (t.text or '').strip() == '>NAME' and t.get('x') is not None),
              None)
    if ov is None:
        continue
    n_ovr += 1
    desig = inst_of[e.get('name')][1]
    bottom = e.get('side') == 'bottom'
    placed = place_ir_element(ov, float(e.get('x', 0)), float(e.get('y', 0)),
                              float(e.get('rot', 0) or 0), bottom)
    d = next((t for t in texts_by_comp.get(index_of[desig], [])
              if t.is_designator), None)
    if d is None:
        no_text.append(desig)
        continue
    dx = abs((d.x_mils - ox) - (float(placed.get('x')) - min_x) / 25.4)
    dy = abs((d.y_mils - oy) - (float(placed.get('y')) - min_y) / 25.4)
    if max(dx, dy) > worst_t:
        worst_t, worst_t_at = max(dx, dy), desig

print('DESIGNATOR TEXT (per-instance overrides)')
check(not no_text, f'every overridden element has a designator text '
                   f'(missing: {no_text[:5]})')
check(worst_t <= TOL_MILS,
      f'{n_ovr} overrides compared with the IR-baked position, worst '
      f'deviation {worst_t:.4f} mils at {worst_t_at}')
manual = sum(1 for c in pcb.components
             if c.raw_record.get('NAMEAUTOPOSITION') == '0')
check(manual >= n_ovr,
      f'{manual} components have designator autoposition = Manual '
      f'(Altium would otherwise re-place the text)')

# --- board-level free text -------------------------------------------------
ir_free = layout.findall('text')
free = [t for t in pcb.texts if t.component_index is None]
print('BOARD-LEVEL TEXT')
check(len(free) == len(ir_free), f'{len(free)} free texts, IR has {len(ir_free)}')
# a multi-line text is a Text Frame in Altium and stores its lines CRLF-
# separated, so both sides are compared line by line
want_txt = sorted(tuple((t.text or '').strip().splitlines()) for t in ir_free)
got_txt = sorted(tuple((t.text_content or '').splitlines()) for t in free)
check(want_txt == got_txt, 'their contents match the IR')
framed = [t for t in free if getattr(t, 'is_frame', False)]
check(len(framed) == sum(1 for t in ir_free
                         if len((t.text or '').strip().splitlines()) > 1),
      f'{len(framed)} of them are Text Frames — exactly the multi-line ones '
      '(a String draws one line, a newline in it is a stray glyph)')
wt = pcb.widestrings_table or {}
check(all(wt.get(t.widestring_index) == t.text_content for t in free),
      'the WideStrings entry of each agrees with the record')

# --- copper ----------------------------------------------------------------
# The real check is CONNECTIVITY, not counts: every pad Altium binds to a net
# must be exactly the pad the IR's <contactref> names. A wrong pad_nets key
# (pad designator vs pin name) or a lost binding shows up here, not in a
# count of tracks.
sigs = layout.findall('signal')
net_name = {i: n.name for i, n in enumerate(pcb.nets)}
print('COPPER')
check(len(net_name) == len(sigs),
      f'{len(pcb.nets)} nets, IR has {len(sigs)} <signal>')

# By PAD SET, not by name: a net inside a channel is written under the name
# Altium's own compiler gives it (SW_DCDC1, not the IR's DCDC1:SW), so the
# spelling legitimately differs while the partition may not.
want_pads, got_pads = {}, {}
for s in sigs:
    want_pads[s.get('name')] = {(inst_of[r.get('element')][1], r.get('pad'))
                                for r in s.findall('contactref')}
for p in pcb.pads:
    if p.net_index is None or p.net_index < 0 or p.component_index is None:
        continue
    got_pads.setdefault(net_name.get(p.net_index), set()).add(
        (pcb.components[p.component_index].designator, p.designator))
got_sets = {frozenset(v) for v in got_pads.values()}
# A signal with no <contactref> at all (bare copper) binds no pad, so there
# is nothing to find on the board side — matching it by pad set would fail on
# the empty set alone.
bad_net = [n for n, pads in want_pads.items()
           if pads and frozenset(pads) not in got_sets]
check(not bad_net,
      f'{sum(len(v) for v in want_pads.values())} pad-to-net bindings match '
      f'the IR contactrefs (wrong nets: {bad_net[:3]})')
if bad_net:
    n = bad_net[0]
    print(f'        {n}: IR-only={sorted(want_pads[n] - got_pads.get(n, set()))[:4]} '
          f'doc-only={sorted(got_pads.get(n, set()) - want_pads[n])[:4]}')

stray = [n for n, prims in (('track', pcb.tracks), ('arc', pcb.arcs),
                            ('fill', pcb.fills), ('text', pcb.texts),
                            ('pad', pcb.pads), ('via', pcb.vias),
                            ('region', pcb.regions))
         if any(getattr(p, 'polygon_index', 65535) != 65535 for p in prims)]
check(not stray,
      f'no primitive claims membership of a pour (0 = "member of polygon #0" '
      f'to Altium, not "none"; wrong: {stray})')

for tag, prims, label in (('line', pcb.tracks, 'track'),
                          ('arc', pcb.arcs, 'arc'),
                          ('via', pcb.vias, 'via')):
    want = sum(len(s.findall(tag)) for s in sigs)
    got = sum(1 for p in prims if p.component_index is None
              and p.net_index is not None and p.net_index >= 0)
    check(got == want, f'{got} routed {label}s on nets, IR has {want}')

# Pours: the IR contour is the pen CENTRELINE, Altium's outline is the copper
# boundary — so the exported outline must be bigger by exactly the pen radius.
poly_bad = []
ir_polys = [(s.get('name'), p) for s in sigs for p in s.findall('polygon')]
check(len(pcb.polygons) == len(ir_polys),
      f'{len(pcb.polygons)} pours, IR has {len(ir_polys)}')
for name, p in ir_polys:
    vs = [(float(v.get('x')), float(v.get('y'))) for v in p.findall('vertex')]
    r = float(p.get('width', 0) or 0) / 2
    wx = (min(v[0] for v in vs) - r - min_x) / 25.4
    wy = (min(v[1] for v in vs) - r - min_y) / 25.4
    hit = False
    for g in pcb.polygons:
        xs = [v.x_mils - ox for v in g.outline]
        ys = [v.y_mils - oy for v in g.outline]
        if abs(min(xs) - wx) <= 1.0 and abs(min(ys) - wy) <= 1.0:
            hit = True
            break
    if not hit:
        poly_bad.append(f'{name} @({wx:.1f},{wy:.1f})')
check(not poly_bad,
      f'each pour outline sits one pen radius outside the IR centreline '
      f'(wrong: {poly_bad[:3]})')
open_vias = sum(1 for v in pcb.vias if not (v.is_tent_top and v.is_tent_bottom))
check(not open_vias,
      f'every via is tented on both sides ({open_vias} left open — an open '
      f'via is a solder trap and the IR carries no field asking for one)')

not_pouring = [g.name for g in pcb.polygons
               if not g.pour_over or g.pour_over_style != 1]
check(not not_pouring,
      f'every pour merges with its own net\'s copper instead of keeping a '
      f'gap from it (wrong: {not_pouring[:3]})')
open_poly = [g.name for g in pcb.polygons
             if not g.outline
             or abs(g.outline[0].x_mils - g.outline[-1].x_mils) > 1e-6
             or abs(g.outline[0].y_mils - g.outline[-1].y_mils) > 1e-6]
check(not open_poly,
      f'every pour outline is closed explicitly, last vertex on the first '
      f'(open: {open_poly})')
arc_bad = []
for g in pcb.polygons:
    for i, v in enumerate(g.outline[:-1]):
        if v.kind != 1:
            continue
        nxt = g.outline[i + 1]
        s = (v.center_x_mils + v.radius_mils * math.cos(math.radians(v.start_angle)),
             v.center_y_mils + v.radius_mils * math.sin(math.radians(v.start_angle)))
        e = (v.center_x_mils + v.radius_mils * math.cos(math.radians(v.end_angle)),
             v.center_y_mils + v.radius_mils * math.sin(math.radians(v.end_angle)))
        fwd = (math.hypot(s[0] - v.x_mils, s[1] - v.y_mils) <= TOL_MILS
               and math.hypot(e[0] - nxt.x_mils, e[1] - nxt.y_mils) <= TOL_MILS)
        bwd = (math.hypot(e[0] - v.x_mils, e[1] - v.y_mils) <= TOL_MILS
               and math.hypot(s[0] - nxt.x_mils, s[1] - nxt.y_mils) <= TOL_MILS)
        if not (fwd or bwd):
            arc_bad.append(f'{g.name}[{i}]')
check(not arc_bad,
      f'every pour arc joins its own vertex to the next one (either angle '
      f'order, both occur in real Altium files) (broken: {arc_bad[:4]})')

# --- holes -----------------------------------------------------------------
# Altium has no hole primitive: a hole is a PAD, and what separates a mounting
# hole from a connector pin is the plating flag, not the geometry.
ir_holes = layout.findall('hole')
free_pads = [p for p in pcb.pads if p.component_index is None]
print('HOLES')
check(len(free_pads) == len(ir_holes),
      f'{len(free_pads)} free pad(s) for {len(ir_holes)} board <hole>(s)')
hole_bad = []
for h in ir_holes:
    d = float(h.get('drill')) / 25.4
    wx = (float(h.get('x')) - min_x) / 25.4
    wy = (float(h.get('y')) - min_y) / 25.4
    if not any(abs(p.x_mils - ox - wx) <= TOL_MILS
               and abs(p.y_mils - oy - wy) <= TOL_MILS
               and abs(p.hole_size_mils - d) <= TOL_MILS
               and abs(p.width_mils - d) <= TOL_MILS
               and p.is_plated is False for p in free_pads):
        hole_bad.append(f'({wx:.1f},{wy:.1f}) d={d:.1f}')
check(not hole_bad,
      f'each is an unplated round pad with no annular ring, where the IR puts '
      f'it (wrong: {hole_bad[:3]})')
unplated_th = [f'{pcb.components[p.component_index].designator}.{p.designator}'
               for p in pcb.pads
               if p.component_index is not None and p.hole_size_mils > 0
               and p.is_plated is not True]
check(not unplated_th,
      f'{sum(1 for p in pcb.pads if p.component_index is not None and p.hole_size_mils > 0)} '
      f'through-hole component pad(s) are PLATED (unplated: {unplated_th[:4]})')

# --- board-level graphics --------------------------------------------------
# Silkscreen and mask artwork that belongs to the board. Counted per layer and
# per primitive kind, which is what catches a layer projection going wrong —
# the artwork itself is 800+ segments, far past eyeballing.
outline_ids = {id(s) for s in layout
               if s.tag in ('line', 'arc') and s.get('layer') == str(LAYER_DIMENSION)}
want_gfx = {}
for el in layout:
    if el.tag not in ('line', 'arc', 'shape') or id(el) in outline_ids:
        continue
    lay, keepout = target_layer(layer_plan, el.get('layer'))
    if lay is None:
        continue
    # keyed by the keepout flag as well: an anti-layer object is the SAME
    # geometry on a copper layer, and only that flag tells the two apart
    if el.tag in ('line', 'arc'):
        k = (el.tag if el.tag == 'arc' else 'track', lay, keepout)
        want_gfx[k] = want_gfx.get(k, 0) + 1
    else:
        for prim in shape_primitives(el):
            k = ({'arc': 'arc', 'fill': 'fill', 'track': 'track'}[prim[0]],
                 lay, keepout)
            want_gfx[k] = want_gfx.get(k, 0) + 1

got_gfx = {}
for kind, prims in (('track', pcb.tracks), ('arc', pcb.arcs), ('fill', pcb.fills)):
    for x in prims:
        if x.component_index is not None:
            continue
        if getattr(x, 'net_index', None) not in (None, -1, 65535):
            continue                      # routed copper, checked above
        k = (kind, int(x.layer) if str(x.layer).isdigit() else x.layer,
             bool(getattr(x, 'is_keepout', False)))
        got_gfx[k] = got_gfx.get(k, 0) + 1

print('BOARD GRAPHICS (silkscreen, mask, keepouts)')
check(want_gfx == got_gfx,
      f'{sum(want_gfx.values())} board-level primitive(s) on the layers the '
      f'plan gives them — IR {sorted(want_gfx.items())[:3]}... vs doc '
      f'{sorted(got_gfx.items())[:3]}...'
      if want_gfx != got_gfx else
      f'{sum(want_gfx.values())} board-level primitive(s), matching the IR '
      f'per layer, kind and keepout flag')
bad_ko = [f'{k}' for k, v in got_gfx.items() if k[2]] if False else []
ko_prims = [x for kind, prims in (('track', pcb.tracks), ('arc', pcb.arcs),
                                  ('fill', pcb.fills))
            for x in prims if getattr(x, 'is_keepout', False)]
check(all(x.keepout_restrictions == 0b11111 for x in ko_prims),
      f'all {len(ko_prims)} keepout(s) forbid everything — track, via, copper, '
      f'SMD pad, TH pad')

# --- 3D bodies -------------------------------------------------------------
# The library is the source of truth here: whatever footprint carries a body
# in the PcbLib must carry it on every instance placed from it.
lib = AltiumPcbLib.from_file(str(OUT / f'{NAME}.PcbLib'))
modelled = {f.name for f in lib.footprints if f.component_bodies}
bodies_of = {}
for b in (pcb.component_bodies or []):
    if b.component_index is not None:
        bodies_of.setdefault(b.component_index, 0)
        bodies_of[b.component_index] += 1
want_body = [inst_of[e.get('name')][1] for e in els
             if fp_of[e.get('name')].get('name') in modelled]
no_body = [d for d in want_body if not bodies_of.get(index_of[d])]
print('3D BODIES')
check(not no_body,
      f'{len(want_body)} placed component(s) whose footprint has a model got '
      f'a body (missing: {no_body[:5]})')
check(len(pcb.models or []) == len(modelled),
      f'{len(pcb.models or [])} models embedded in the PcbDoc, the PcbLib has '
      f'{len(modelled)} modelled footprint(s)')

# --- design rules ----------------------------------------------------------
print('RULES')
ir_rules = layout.find('rules')
ir_attr = dict(ir_rules.attrib) if ir_rules is not None else {}
generic = {}
for r in pcb.rules:
    d = r.raw_record
    if d.get('RULEKIND') in DRC_RULES and d.get('ENABLED') == 'TRUE' \
            and d.get('SCOPE1EXPRESSION') == 'All' \
            and d.get('SCOPE2EXPRESSION') == 'All':
        attr, field = DRC_RULES[d['RULEKIND']]
        generic[attr] = d.get(field)
bad_dr = []
for attr, want in ir_attr.items():
    got = generic.get(attr)
    if got is None or abs(float(str(got).replace('mil', '')) * 25.4
                          - float(want)) > 1.0:
        bad_dr.append(f'{attr}: IR {want}µm vs doc {got}')
check(not bad_dr,
      f'all {len(ir_attr)} IR <rules> numbers land on their generic Altium '
      f'rule (wrong: {bad_dr[:3]})')

# Rules6 is binary: the 2-byte record leader is the rule KIND's id, not
# padding. Zeroing it made every rule read back as kind 0 (Clearance) and
# crashed Altium, so: one leader per kind, and never two kinds sharing one.
leader_of = {}
leader_bad = []
for r in pcb.rules:
    kind = r.raw_record.get('RULEKIND')
    lead = bytes(r.record_leader or b'')
    if len(lead) != 2:
        leader_bad.append(f'{kind}: {lead!r}')
    elif leader_of.setdefault(kind, lead) != lead:
        leader_bad.append(f'{kind}: {lead!r} vs {leader_of[kind]!r}')
dupes = [k for k, v in leader_of.items()
         if sum(1 for o in leader_of.values() if o == v) > 1]
check(not leader_bad and not dupes,
      f'every rule carries its kind id as record leader, one per kind '
      f'(bad: {leader_bad[:3]}, shared: {dupes[:4]})')

# A rule that contradicts the geometry shipped beside it is our defect, not a
# finding about the design: the stock document asks for 50 mil vias, 10 mil
# tracks and 100 mil holes, and tolmach has none of those.
def _mil_of(s):
    try:
        return float(str(s).replace('mil', '').strip())
    except (TypeError, ValueError):
        return None


rule_of = {r.raw_record.get('RULEKIND'): r.raw_record for r in pcb.rules}
oc = (rule_of.get('Clearance') or {}).get('OBJECTCLEARANCES') or ''
check('Hole:0' not in oc.replace(' ', ''),
      'the Clearance rule does not hand out zero copper-to-HOLE clearances '
      '(the stock table does, and it shorted the pours onto the mounting '
      'holes)')
w_rule, h_rule, v_rule = (rule_of.get('Width'), rule_of.get('HoleSize'),
                          rule_of.get('RoutingVias'))
widths = [t.width_mils for t in pcb.tracks if 1 <= int(t.layer) <= 32] + \
         [a.width_mils for a in pcb.arcs if 1 <= int(a.layer) <= 32]
holes = [p.hole_size_mils for p in pcb.pads if p.hole_size_mils > 0]
over = []
if w_rule and widths and max(widths) > _mil_of(w_rule['MAXLIMIT']) + TOL_MILS:
    over.append(f'track {max(widths):.1f} > Width MAX {w_rule["MAXLIMIT"]}')
if h_rule and holes and max(holes) > _mil_of(h_rule['MAXLIMIT']) + TOL_MILS:
    over.append(f'hole {max(holes):.1f} > HoleSize MAX {h_rule["MAXLIMIT"]}')
if v_rule and pcb.vias:
    for v in pcb.vias:
        if not (_mil_of(v_rule['MINWIDTH']) - TOL_MILS <= v.diameter_mils
                <= _mil_of(v_rule['MAXWIDTH']) + TOL_MILS
                and _mil_of(v_rule['MINHOLEWIDTH']) - TOL_MILS <= v.hole_size_mils
                <= _mil_of(v_rule['MAXHOLEWIDTH']) + TOL_MILS):
            over.append(f'via {v.diameter_mils:.1f}/{v.hole_size_mils:.1f} '
                        f'outside RoutingVias')
            break
check(not over,
      f'no object violates a rule we wrote ourselves — Width/HoleSize/'
      f'RoutingVias bracket the real geometry ({over[:2]})')

connect = sorted((r.raw_record for r in pcb.rules
                  if r.raw_record.get('RULEKIND') == 'PolygonConnect'),
                 key=lambda d: int(d.get('PRIORITY') or 0))
via_rule = [d for d in connect if d.get('SCOPE1EXPRESSION') == 'isVia']
check(len(via_rule) == 1 and via_rule[0].get('CONNECTSTYLE') == 'Direct'
      and int(via_rule[0]['PRIORITY']) == min(int(d['PRIORITY']) for d in connect),
      'vias connect directly, at the highest priority of any PolygonConnect '
      '(a relief on a via is never wanted)')

fallback = [d for d in connect if d.get('SCOPE1EXPRESSION') == 'All'
            and d.get('SCOPE2EXPRESSION') == 'All']
check(len(fallback) == 1 and fallback[0].get('CONNECTSTYLE') == 'Relief'
      and int(fallback[0]['PRIORITY']) == max(int(d['PRIORITY']) for d in connect),
      'the generic Relief rule is the weakest fallback, as Altium ships it')

named = {d.get('SCOPE2EXPRESSION'): d for d in connect}
bad_th = []
for g, (net, el) in zip(pcb.polygons, ir_polys):
    key = f"IsNamedPolygon('{g.name}')"
    want_direct = el.get('thermals') == '0'
    rule = named.get(key)
    if want_direct and (rule is None or rule.get('CONNECTSTYLE') != 'Direct'):
        bad_th.append(f'{g.name}: thermals=0 but no Direct rule')
    if not want_direct and rule is not None:
        bad_th.append(f'{g.name}: no thermals=0 in the IR yet a rule exists')
check(not bad_th,
      f'{sum(1 for _n, e in ir_polys if e.get("thermals") == "0")} pour(s) with '
      f'thermals="0" get their own Direct rule, the rest keep the board '
      f'default (wrong: {bad_th[:3]})')

# --- ECO link --------------------------------------------------------------
# A component's id is a PATH from the top sheet down (BC2087 ground truth:
# `\<sheet symbol>\<component>` on a child sheet, `\<component>` at the top),
# and its room is named in SOURCEHIERARCHICALPATH.
# ONE child document serves every channel, so a part is keyed by the name it
# ends up with on the board — the channel format — and not by the canonical
# designator it wears on the shared sheet (where the module's R1 and the top
# level's R1 would otherwise collide).
sch = AltiumSchDoc(str(OUT / f'{NAME}.SchDoc'))
sch_uid, sch_room, sch_item = {}, {}, {}
objs = sch.objects
for d in sch.designators:
    owner = objs[d.owner_index]
    sch_uid[d.text] = '\\' + owner.unique_id
    sch_room[d.text] = 'TopLevel'
    sch_item[d.text] = owner.design_item_id
_child_cache = {}
for ss in sch.sheet_symbols:
    fname = ss.file_name.text
    child = _child_cache.setdefault(fname, AltiumSchDoc(str(OUT / fname)))
    cobjs = child.objects
    room = ss.sheet_name.text
    for d in child.designators:
        name = channel_designator(room, d.text)
        owner = cobjs[d.owner_index]
        sch_uid[name] = f'\\{ss.unique_id}\\{owner.unique_id}'
        sch_room[name] = f'TopLevel\\{room}'
        sch_item[name] = owner.design_item_id
print('ECO LINK')
# A board-only element (Eagle logo: no part in the schematic) is a PCB-only
# component in Altium and carries no link BY DEFINITION — the link is owed
# by everything that does have a schematic instance.
want_link = {inst_of[e.get('name')][1] for e in els
             if inst_of[e.get('name')][0] is not None}
linked = {c.designator for c in pcb.components if c.source_unique_id}
check(want_link <= linked,
      f'{len(linked)}/{len(pcb.components)} components carry SOURCEUNIQUEID, '
      f'every schematic-born one among them '
      f'(missing: {sorted(want_link - linked)[:5]})')
bad = [c.designator for c in pcb.components
       if sch_uid.get(c.designator) and c.source_unique_id != sch_uid[c.designator]]
check(not bad, f'SOURCEUNIQUEID is the path down to the SchDoc component '
               f'(bad: {bad[:5]})')
bad_room = [c.designator for c in pcb.components
            if sch_room.get(c.designator)
            and c.raw_record.get('SOURCEHIERARCHICALPATH') != sch_room[c.designator]]
check(not bad_room,
      f'SOURCEHIERARCHICALPATH names the sheet the component lives on '
      f'(bad: {bad_room[:5]})')
check(all(c.source_designator == c.designator for c in pcb.components),
      'SOURCEDESIGNATOR matches the designator')
# Altium compares Design Item IDs on ECO, and the board states it in
# SOURCELIBREFERENCE. Sending the SchLib entry name (`C`) instead of the
# DbLib part number (`C_C0603`) made the first real ECO offer to change the
# item id of every part on the board.
bad_item = [c.designator for c in pcb.components
            if c.designator in sch_item
            and c.raw_record.get('SOURCELIBREFERENCE') != sch_item[c.designator]]
check(not bad_item,
      f'SOURCELIBREFERENCE is the schematic component\'s Design Item ID '
      f'(bad: {bad_item[:5]})')

print()
print('BOARD VERIFY: ' + ('OK' if not fails else f'{len(fails)} FAILURE(S)'))
sys.exit(1 if fails else 0)
