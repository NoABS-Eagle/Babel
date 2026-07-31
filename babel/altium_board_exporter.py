"""IR <layout> -> Altium .PcbDoc.

Done so far: board origin, outline, layer stack, component placement and the
ECO link back to the SchDoc; component text overrides and board-level labels;
the board cutouts a footprint brings with it; COPPER (every <signal>'s nets,
pad bindings, tracks, arcs, vias and pours); the DRC <rules> including polygon
connect styles; board-level SILKSCREEN and mask artwork; mounting holes; and KEEPOUTS (an
anti-layer in the IR, a flag on an ordinary primitive in Altium). Board-level
<attr> stays in the IR by decision, not by omission — see _NOT_EXPORTED.
Anything else the layout ever grows is logged as deferred, never silently
dropped.

Placement math — the one thing that must be right before anything is laid on
top of it (mirrors altium_project_parser's import findings):

  IR      (ir_util.place_ir_element): mirror about the Y axis, THEN rotate.
  Altium  (altium_pcbdoc_builder_placement._forward_transform_point):
          mirror about the X axis, THEN rotate.

  Reflect_Y = Rot(180) . Reflect_X, so for a BOTTOM element the angle handed
  to Altium is the IR angle + 180. This is the exact inverse of the import
  rule the user confirmed on real hardware (project_altium_board_first_pass:
  "bottom-компоненты требуют ДОПОЛНИТЕЛЬНЫХ +180°"), now derived a second,
  independent way from the library's own forward transform — two agreeing
  sources, not a guess. verify_altium_board.py checks it numerically on
  every pad of every component.

Coordinates: IR board space is centred on zero (negative coordinates are
normal); Altium wants the board on its sheet. The whole layout is therefore
translated so the outline's lower-left corner sits at _MARGIN_MILS, and the
board origin is set to that same corner — so the importer, which subtracts
`board.origin`, recovers the IR geometry up to that one translation.
"""
import hashlib
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from altium_monkey import AltiumPcbLib, PcbDocBuilder
from altium_monkey.altium_board import AltiumBoardOutline, BoardOutlineVertex
from altium_monkey.altium_pcbdoc_layer_stack_builder import (
    PcbDocCopperLayerTemplate, PcbDocDielectricTemplate, PcbDocLayerStackTemplate)
# The placement path's own local->board transform. Imported rather than
# re-derived so an override lands through the EXACT same math that placed the
# footprint's library copy — see _apply_text_overrides.
from altium_monkey.altium_pcbdoc_builder_placement import (
    _forward_transform_angle, _forward_transform_point)
from altium_monkey.altium_pcb_rule import AltiumPcbRule
from altium_monkey.altium_pcbdoc_builder import PcbDocRulesData
from altium_monkey.altium_record_pcb__polygon import PcbPolygonVertex
from altium_monkey.altium_record_types import PcbLayer

from babel import import_log
from babel import altium_layers
from babel.altium_exporter import (DRC_RULES, _altium_arc_angles,
                                   _pcb_justif, _pcb_layer, fp_name,
                                   mark_keepout, stroke_box,
                                   npth_pad_kwargs, shape_primitives,
                                   target_layer)
from babel.ir_util import (LAYER_DIMENSION, arc_center, arc_params,
                           chain_loops, component_gates, designator_resolver,
                           instance_footprint, offset_contour, parse_stack,
                           resolved_attrs)


# The board's lower-left corner on the Altium sheet.
_MARGIN_MILS = 1000.0

# Chain-building tolerance for the outline, in µm. IR board coordinates are
# integer µm, so anything above 1 µm is a genuinely open contour.
_JOIN_TOL_UM = 1.0

# Dielectric constant used when the IR stack formula carries none. The IR
# formula's ε is optional (ir_util.parse_stack); Altium's stack always has a
# number, so one must be chosen — this is altium_monkey's own FR-4 default.
_DEFAULT_EPS = 4.8


def _mil(um):
    """IR µm -> Altium mils. Deliberately NOT rounded (altium_exporter._mils
    rounds to whole mils, which is 25.4 µm — coarser than the IR grid)."""
    return float(um) / 25.4


# ---------------------------------------------------------------------------
# Board outline: IR line/arc soup on LAYER_DIMENSION -> one ordered loop
# ---------------------------------------------------------------------------

def _outline_segments(layout_el):
    """Every <line>/<arc> on the dimension layer as (x1, y1, x2, y2, curve)."""
    segs = []
    for el in layout_el:
        if el.tag not in ('line', 'arc'):
            continue
        try:
            layer = int(el.get('layer', '0'))
        except ValueError:
            continue
        if layer != LAYER_DIMENSION:
            continue
        segs.append((float(el.get('x1')), float(el.get('y1')),
                     float(el.get('x2')), float(el.get('y2')),
                     float(el.get('curve', 0) or 0)))
    return segs


def _chain(segs):
    """The board's ONE closed outline loop. Assembly itself lives in
    ir_util.chain_loops (a footprint's milling contour uses the same walk);
    what is specific here is that anything but exactly one closed ring is a
    hard reject — that is not a board."""
    if not segs:
        raise ValueError('layout: no geometry on the dimension layer '
                         f'({LAYER_DIMENSION}) — no board outline to export')
    loops, leftover = chain_loops(segs, _JOIN_TOL_UM)
    if leftover:
        raise ValueError(f'board outline is open — {len(leftover)} segment(s) '
                         'do not join into a closed ring')
    if len(loops) != 1:
        raise ValueError(f'board outline is {len(loops)} closed loops; the '
                         'board is one')
    return loops[0]


def _outline_vertices(loop, dx_um, dy_um):
    """Ordered loop -> Altium BoardOutlineVertex list, translated by
    (dx, dy) µm.

    Altium draws an arc CCW from start_angle to end_angle, so a segment we
    traverse clockwise (curve < 0) must be declared with its angles SWAPPED.
    That is the writing side of the import's finding that forward and
    backward arcs coexist in one real file — a property of the format, not
    of a particular file."""
    out = []
    for x1, y1, x2, y2, curve in loop:
        px, py = _mil(x1 + dx_um), _mil(y1 + dy_um)
        if not curve:
            out.append(BoardOutlineVertex.line(px, py))
            continue
        c = arc_center(x1, y1, x2, y2, curve)
        if c is None:
            out.append(BoardOutlineVertex.line(px, py))
            continue
        cx, cy, r = c
        a1 = math.degrees(math.atan2(y1 - cy, x1 - cx)) % 360.0
        a2 = math.degrees(math.atan2(y2 - cy, x2 - cx)) % 360.0
        start, end = (a1, a2) if curve > 0 else (a2, a1)
        out.append(BoardOutlineVertex.arc(
            px, py,
            center_mils=(_mil(cx + dx_um), _mil(cy + dy_um)),
            radius_mils=_mil(r),
            start_angle_degrees=start, end_angle_degrees=end))
    return out


def _outline_bbox(loop):
    """(min_x, min_y, max_x, max_y) µm of the loop, arc bulges included —
    an arc can stick out past both of its endpoints."""
    xs, ys = [], []
    for x1, y1, x2, y2, curve in loop:
        xs += [x1, x2]
        ys += [y1, y2]
        c = arc_center(x1, y1, x2, y2, curve)
        if c is None:
            continue
        cx, cy, r = c
        a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
        sweep = curve
        for axis_deg, px, py in ((0, cx + r, cy), (90, cx, cy + r),
                                 (180, cx - r, cy), (270, cx, cy - r)):
            # does this cardinal point lie on the swept arc?
            d = (axis_deg - a1) % 360.0
            if sweep < 0:
                d -= 360.0
            if 0 <= d <= sweep or sweep <= d <= 0:
                xs.append(px)
                ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Layer stack: the IR formula, verbatim — never a template's default numbers
# ---------------------------------------------------------------------------

def _stack_template(stack_str, layout_name):
    coppers, dielectrics = parse_stack(stack_str)
    n = len(coppers)
    layers = []
    for i, um in enumerate(coppers):
        if i == 0:
            legacy, v7, name, placement, orient = 1, 16777217, 'Top Layer', 1, None
        elif i == n - 1:
            legacy, v7, name, placement, orient = (32, 16842751, 'Bottom Layer',
                                                   2, 1)
        else:
            legacy = i + 1
            v7, name, placement, orient = (16777216 + legacy,
                                           f'Mid Layer {i}', 0, None)
        layers.append(PcbDocCopperLayerTemplate(
            legacy_layer_id=legacy, v7_layer_id=v7, name=name,
            copper_thickness_mils=_mil(um), component_placement=placement,
            copper_orientation=orient))
    diels = []
    for i, (um, eps, tand) in enumerate(dielectrics):
        if eps is None:
            import_log.log(layout_name, 'stack',
                           f'dielectric {i + 1}: IR carries no ε, '
                           f'Altium needs one — wrote {_DEFAULT_EPS}')
        diels.append(PcbDocDielectricTemplate(
            name=f'Dielectric {i + 1}', thickness_mils=_mil(um),
            dielectric_constant=eps if eps is not None else _DEFAULT_EPS,
            material='FR-4', dielectric_type=0, loss_tangent=tand))
    return PcbDocLayerStackTemplate(
        name=f'{n}-layer', copper_layers=tuple(layers),
        dielectrics_between=tuple(diels))


# ---------------------------------------------------------------------------
# Fills: working around a double-counted rotation in altium_monkey
# ---------------------------------------------------------------------------

def _fix_placed_fills(builder, idx, footprint, position_mils, alt_rot, bottom):
    """An Altium Fill is an AXIS-ALIGNED box (pos1, pos2) plus a rotation
    about its OWN centre — the two facts are independent.

    `altium_pcbdoc_builder_placement._add_placed_fills` transforms BOTH
    corner points by the component placement (which already bakes the
    component angle into the box) AND adds the component angle to the fill's
    own rotation — so the rectangle is rotated twice. At 180° the extra turn
    is invisible (a box is symmetric about its centre) and at 90/270° it
    exactly undoes the corner transform: the box lands in the right place but
    axis-aligned, i.e. NOT rotated. That is what SOD323's silk rectangles
    showed on tolmach (ZD2, placed at 270°) while every line and arc of the
    same footprint came out right.

    Correct placement: move the CENTRE, keep the box size, add the angle
    once. Rewritten here rather than in the library because altium_monkey is
    a third-party package (same policy as _sym_add_arc in altium_exporter).
    """
    placed = [f for f in builder.fills if f.component_index == idx]
    if len(placed) != len(footprint.fills):
        raise ValueError(f'{len(placed)} placed fills for component {idx} but '
                         f'{len(footprint.fills)} in the footprint — the '
                         'placement path changed, re-check _fix_placed_fills')
    cx, cy = position_mils
    for src, dst in zip(footprint.fills, placed):
        mx = (src.pos1_x_mils + src.pos2_x_mils) / 2
        my = (src.pos1_y_mils + src.pos2_y_mils) / 2
        hw = abs(src.pos2_x_mils - src.pos1_x_mils) / 2
        hh = abs(src.pos2_y_mils - src.pos1_y_mils) / 2
        bx, by = _forward_transform_point(mx, my, cx, cy, alt_rot, bottom)
        dst.pos1_x = dst._to_internal_units(bx - hw)
        dst.pos1_y = dst._to_internal_units(by - hh)
        dst.pos2_x = dst._to_internal_units(bx + hw)
        dst.pos2_y = dst._to_internal_units(by + hh)
        dst.rotation = _forward_transform_angle(src.rotation, alt_rot, bottom)
    return len(placed)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

# The two placeholders a footprint's own text can be, and the flag the placed
# Altium text carries. Any other '>ATTR' placeholder is not emitted by the
# library exporter either, so there is nothing here to move.
_PLACEHOLDER_SLOT = {'>NAME': 'is_designator', '>VALUE': 'is_comment'}


# Which placeholder governs which visibility flag on the placed component.
_PLACEHOLDER_FLAG = {'>NAME': 'NAMEON', '>VALUE': 'COMMENTON'}


def _apply_text_visibility(builder, idx, el, fp_el):
    """Show a component's designator/comment only where the IR says so.

    What is DRAWN is what the FOOTPRINT declares as a placeholder
    (`>NAME`/`>VALUE`); the element may hide it (Eagle smash + hide). Altium
    expresses this as NAMEON/COMMENTON on the component, and the text object
    itself STAYS: deleting it makes Altium invent a replacement and draw
    "Designator1" next to the part (seen on step4's SCR1 — a screw whose
    footprint declares nothing).
    """
    hidden_here = 0
    for content, flag in _PLACEHOLDER_FLAG.items():
        hidden = any((t.text or '').strip() == content and t.get('hidden') == 'yes'
                     for t in el.findall('text'))
        shown = (not hidden) and any(
            (t.text or '').strip() == content
            for t in list(fp_el.findall('text')) + list(el.findall('text')))
        builder.components[idx].raw_record[flag] = 'TRUE' if shown else 'FALSE'
        hidden_here += 0 if shown else 1
    return hidden_here


def _text_stroke_mils(t_el, height_mils):
    """IR `ratio` (stroke thickness as a % of height, Eagle's own field) ->
    Altium stroke width. No ratio -> Altium's usual 10%."""
    try:
        ratio = float(t_el.get('ratio')) / 100.0
    except (TypeError, ValueError):
        ratio = 0.10
    return max(height_mils * ratio, 0.5)


def _apply_text_overrides(builder, idx, el, fp_el, position_mils, alt_rot,
                          bottom):
    """Per-instance >NAME/>VALUE overrides on a board element.

    A placed component's texts are CLONES of the footprint's — right where
    the library put them. An Eagle element that had its name moved carries
    its own <text> child (LOCAL footprint coordinates, like the schematic's
    instance texts), and until now the board export ignored those entirely,
    so 46 of tolmach's designators sat at the library default.

    Which placeholders are shown is still decided by the FOOTPRINT (the same
    canon eagle_board_exporter follows: the library is the source of truth);
    the element only overrides position/angle/size/justification, or hides.
    """
    overrides = {(t.text or '').strip(): t for t in el.findall('text')}
    if not overrides:
        return 0
    texts = [t for t in builder.texts if t.component_index == idx]
    cx, cy = position_mils
    moved = 0
    for fpt in fp_el.findall('text'):
        content = (fpt.text or '').strip()
        slot = _PLACEHOLDER_SLOT.get(content)
        ov = overrides.get(content)
        if slot is None or ov is None:
            continue
        target = next((t for t in texts if getattr(t, slot)), None)
        if target is None:
            continue
        if ov.get('hidden') == 'yes':
            # visibility is a flag on the component (_apply_text_visibility),
            # never a missing object — Altium replaces a missing designator
            # with one of its own
            continue
        if ov.get('x') is None:            # nothing moved — the clone is right
            continue

        lx, ly = float(ov.get('x', 0)), float(ov.get('y', 0))
        ax, ay = _forward_transform_point(_mil(lx), _mil(ly), cx, cy,
                                          alt_rot, bottom)
        target.x = target._to_internal_units(ax)
        target.y = target._to_internal_units(ay)
        target.rotation = _forward_transform_angle(
            float(ov.get('rot', 0) or 0), alt_rot, bottom)
        if ov.get('size'):
            h = _mil(ov.get('size'))
            target.height = target._to_internal_units(h)
            target.stroke_width = target._to_internal_units(
                _text_stroke_mils(ov, h))
        # justification is carried across as authored — the same rule the
        # schematic export settled on (no re-derivation per quadrant)
        target.textbox_rect_justification = int(_pcb_justif(ov.get('align')))
        target.is_justification_valid = True
        moved += 1

    if moved:
        # Altium re-places a designator whose autoposition is not Manual,
        # which would undo everything above.
        builder.components[idx].raw_record['NAMEAUTOPOSITION'] = '0'
    return moved


def _emit_free_graphics(builder, layout_el, dx_um, dy_um, layer_plan, outline_ids):
    """Board-level <line>/<arc>/<shape> — silkscreen and mask artwork that
    belongs to the BOARD, not to any footprint.

    The outline's own segments are excluded by id — they are already the board
    shape. An ANTI-layer object goes out as the same geometry on the copper
    layer it names, flagged as a keepout (altium_exporter.target_layer).
    """
    handled, n = set(), 0
    pools = (builder.tracks, builder.arcs, builder.fills)
    for el in layout_el:
        if el.tag not in ('line', 'arc', 'shape') or id(el) in outline_ids:
            continue
        layer, keepout = target_layer(layer_plan, el.get('layer'))
        if layer is None:
            continue                       # counted by _log_deferred instead
        before = [len(p) for p in pools]
        if el.tag == 'line':
            builder.add_track(
                (_mil(float(el.get('x1')) + dx_um),
                 _mil(float(el.get('y1')) + dy_um)),
                (_mil(float(el.get('x2')) + dx_um),
                 _mil(float(el.get('y2')) + dy_um)),
                width_mils=_mil(el.get('width', 0)), layer=layer)
        elif el.tag == 'arc':
            p = arc_params(float(el.get('x1', 0)), float(el.get('y1', 0)),
                           float(el.get('x2', 0)), float(el.get('y2', 0)),
                           float(el.get('curve', 0) or 0))
            if p is None:
                continue
            cx, cy, r, start, sweep = p
            a1, a2 = _altium_arc_angles(start, sweep)
            builder.add_arc(center_mils=(_mil(cx + dx_um), _mil(cy + dy_um)),
                            radius_mils=_mil(r), start_angle=a1, end_angle=a2,
                            width_mils=_mil(el.get('width', 0)), layer=layer)
        else:
            for prim in shape_primitives(el):
                if prim[0] == 'arc':
                    _, cx, cy, r, a1, a2, w = prim
                    builder.add_arc(
                        center_mils=(_mil(cx + dx_um), _mil(cy + dy_um)),
                        radius_mils=_mil(r), start_angle=a1, end_angle=a2,
                        width_mils=_mil(w), layer=layer)
                elif prim[0] == 'fill':
                    _, x1, y1, x2, y2, rot = prim
                    builder.add_fill((_mil(x1 + dx_um), _mil(y1 + dy_um)),
                                     (_mil(x2 + dx_um), _mil(y2 + dy_um)),
                                     layer=layer, rotation_degrees=rot)
                else:
                    _, x1, y1, x2, y2, w = prim
                    builder.add_track((_mil(x1 + dx_um), _mil(y1 + dy_um)),
                                      (_mil(x2 + dx_um), _mil(y2 + dy_um)),
                                      width_mils=_mil(w), layer=layer)
        if keepout:
            # add_* returns the builder, not the record, so the new tail of
            # each pool is what this element produced.
            for pool, was in zip(pools, before):
                for rec in pool[was:]:
                    mark_keepout(rec)
        handled.add(id(el))
        n += 1
    return handled, n


def _emit_holes(builder, layout_el, dx_um, dy_um):
    """Board-level <hole> — the mounting holes, which belong to the board
    rather than to any footprint."""
    n = 0
    for el in layout_el.findall('hole'):
        builder.add_pad(position_mils=(_mil(float(el.get('x')) + dx_um),
                                       _mil(float(el.get('y')) + dy_um)),
                        **npth_pad_kwargs(el))
        n += 1
    return n


def _multiline(text, height_mils):
    """(content, frame size) for one IR text.

    A PCB String draws ONE line: a newline inside it renders as a stray glyph,
    not as a second line. Altium's multi-line object is the same String with
    `is_frame` set, its lines separated by CRLF and a text box around them
    (ground truth: PiMX8MPIODB_r0.1.PcbDoc, a licence notice stored exactly
    that way). Single-line text keeps the plain String — nothing to frame.
    """
    lines = (text or '').strip().splitlines()
    if len(lines) < 2:
        return (text or '').strip(), None
    return '\r\n'.join(lines), stroke_box(lines, height_mils)


def _emit_free_texts(builder, layout_el, dx_um, dy_um, layer_plan):
    """Board-level <text> — silkscreen labels that belong to the board, not
    to any footprint."""
    n = 0
    for t in layout_el.findall('text'):
        layer = _pcb_layer(layer_plan, t.get('layer'))
        if layer is None:
            import_log.log(layout_el.get('name', 'main'), 'text',
                           f'no Altium layer for IR layer {t.get("layer")} — '
                           f'text {(t.text or "").strip()!r} dropped')
            continue
        height = _mil(t.get('size', '1000'))
        content, frame_size = _multiline(t.text, height)
        builder.add_text(
            text=content,
            position_mils=(_mil(float(t.get('x', 0)) + dx_um),
                           _mil(float(t.get('y', 0)) + dy_um)),
            height_mils=height,
            layer=layer,
            rotation_degrees=float(t.get('rot', 0) or 0),
            stroke_width_mils=_text_stroke_mils(t, height),
            is_mirrored=t.get('mirror') == '1',
            text_justification=int(_pcb_justif(t.get('align'))),
            is_frame=frame_size is not None,
            frame_size_mils=frame_size,
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# Copper
# ---------------------------------------------------------------------------

def _copper_layer(ir_layer, n_copper, where):
    """IR copper layer number -> Altium copper layer id.

    IR numbers copper top-down: 1 = top, 2..N-1 = inner, -1 = bottom
    (ir_schema.md). Altium numbers it the same way — 1 Top, 2..31 Mid Layer
    1..30, 32 Bottom — so the inner numbers pass straight through; only the
    bottom needs naming. A layer the declared stack does not have is a hard
    reject: copper silently landing on the wrong layer is a short.
    """
    n = int(ir_layer)
    if n == -1:
        return 32
    if n == 1:
        return 1
    if 2 <= n <= n_copper - 1:
        return n
    raise ValueError(f'{where}: copper on IR layer {ir_layer}, but the stack '
                     f'declares {n_copper} copper layer(s)')


# Altium's "belongs to no polygon" sentinel. altium_monkey's add_* helpers
# leave polygon_index at 0, which Altium reads as MEMBER OF POLYGON #0: the
# routing was drawn as part of the first pour and could not be selected on
# its own. Ground truth — every track, arc, text and pad of a real board
# (testData/altium/IND/RLT504_117C.PcbDoc) carries 65535; only a pour's own
# fill regions carry a real index, and we never write those (Altium
# regenerates a pour from its outline).
_NO_POLYGON = 65535


def _clear_pour_membership(builder):
    for prims in (builder.tracks, builder.arcs, builder.fills, builder.texts,
                  builder.pads, builder.vias, builder.regions):
        for p in prims:
            if getattr(p, 'polygon_index', None) != _NO_POLYGON:
                p.polygon_index = _NO_POLYGON


def _pad_nets(layout_el, net_names):
    """{designator: {pad: net}} — the <contactref> half of a signal, which is
    what actually binds a placed pad to its net in the PcbDoc."""
    out = {}
    for sig in layout_el.findall('signal'):
        net = net_names(sig.get('name'))
        for ref in sig.findall('contactref'):
            out.setdefault(ref.get('element'), {})[ref.get('pad')] = net
    return out


def _polygon_vertices(vs, width_um, dx_um, dy_um, where):
    """IR <polygon> centreline vertices -> the Altium polygon's OUTLINE.

    The IR contour is the PEN CENTRELINE plus a width (ir_schema.md "МОДЕЛЬ
    ПЕРА"); Altium's polygon outline is the copper BOUNDARY — the same thing
    the importer un-offsets inward by track_width/2 to get back here. So the
    export offsets outward by exactly that, through the same ir_util pen
    model the KiCad zone exporter uses.
    """
    verts = [(float(v.get('x')), float(v.get('y')),
              float(v.get('curve', 0) or 0)) for v in vs]
    out = []
    for kind, x1, y1, x2, y2, curve in offset_contour(verts, width_um / 2):
        px, py = _mil(x1 + dx_um), _mil(y1 + dy_um)
        c = arc_center(x1, y1, x2, y2, curve) if curve else None
        if c is None:
            out.append(PcbPolygonVertex(px, py))
            continue
        cx, cy, r = c
        a1 = math.degrees(math.atan2(y1 - cy, x1 - cx)) % 360.0
        a2 = math.degrees(math.atan2(y2 - cy, x2 - cx)) % 360.0
        start, end = (a1, a2) if curve > 0 else (a2, a1)
        out.append(PcbPolygonVertex(
            px, py, kind=1, radius_mils=_mil(r),
            start_angle=start, end_angle=end,
            center_x_mils=_mil(cx + dx_um), center_y_mils=_mil(cy + dy_um),
            has_center=True))
    # An Altium polygon outline is CLOSED EXPLICITLY: its last vertex repeats
    # the first point. Ground truth — GND_L01_P007 on
    # testData/altium/IND/RLT504_117C.PcbDoc: 27 vertices for 26 segments,
    # the 27th sitting exactly on the 1st. Without it Altium consumes the
    # last vertex as the terminator and the closing corner comes out wrong,
    # which is what tolmach's GND and VDD_3V3 pours showed.
    if out:
        out.append(PcbPolygonVertex(out[0].x_mils, out[0].y_mils))
    return out


def _emit_copper(builder, layout_el, dx_um, dy_um, n_copper, net_names):
    """Tracks, arcs, vias and pours of every <signal>. The nets themselves
    are created BEFORE the components are placed (a pad binds to a net by
    name), so this only draws."""
    n = {'track': 0, 'arc': 0, 'via': 0, 'poly': 0}
    pours = []
    for sig in layout_el.findall('signal'):
        net = net_names(sig.get('name'))
        for el in sig:
            if el.tag == 'line':
                builder.add_track(
                    (_mil(float(el.get('x1')) + dx_um),
                     _mil(float(el.get('y1')) + dy_um)),
                    (_mil(float(el.get('x2')) + dx_um),
                     _mil(float(el.get('y2')) + dy_um)),
                    width_mils=_mil(el.get('width', 0)),
                    layer=_copper_layer(el.get('layer'), n_copper,
                                        f'signal {net}'),
                    net=net)
                n['track'] += 1
            elif el.tag == 'arc':
                x1, y1 = float(el.get('x1')), float(el.get('y1'))
                x2, y2 = float(el.get('x2')), float(el.get('y2'))
                curve = float(el.get('curve', 0) or 0)
                c = arc_center(x1, y1, x2, y2, curve)
                if c is None:
                    continue
                cx, cy, r = c
                a1 = math.degrees(math.atan2(y1 - cy, x1 - cx)) % 360.0
                a2 = math.degrees(math.atan2(y2 - cy, x2 - cx)) % 360.0
                start, end = (a1, a2) if curve > 0 else (a2, a1)
                builder.add_arc(
                    center_mils=(_mil(cx + dx_um), _mil(cy + dy_um)),
                    radius_mils=_mil(r), start_angle=start, end_angle=end,
                    width_mils=_mil(el.get('width', 0)),
                    layer=_copper_layer(el.get('layer'), n_copper,
                                        f'signal {net}'),
                    net=net)
                n['arc'] += 1
            elif el.tag == 'via':
                builder.add_via(
                    position_mils=(_mil(float(el.get('x')) + dx_um),
                                   _mil(float(el.get('y')) + dy_um)),
                    diameter_mils=_mil(el.get('diameter', 0)),
                    hole_size_mils=_mil(el.get('drill', 0)),
                    net=net,
                    # A via is COVERED by solder mask on both sides. The IR
                    # has no per-via tenting field, so this is a policy, and
                    # it is the one every source agrees on: Eagle tents these
                    # vias itself (mlViaStopLimit 0.5 mm against a 0.3 mm
                    # drill), and of the 796 vias on the two real Altium
                    # boards in testData exactly 3 are open. An open via is a
                    # solder trap; if a design ever needs one (a via used as
                    # a test point) that fact has to reach the IR first.
                    is_tent_top=True, is_tent_bottom=True)
                n['via'] += 1
            elif el.tag == 'polygon':
                width = float(el.get('width', 0) or 0)
                # IR `fill` is ONE 0..100 scale (ir_schema.md): 100 solid,
                # 0 outline only, in between a hatch density Altium has no
                # number for — it only knows Solid/Hatched/None.
                name = f'{net}_{el.get("layer")}_{n["poly"]}'
                fill = int(el.get('fill', 100) or 100)
                hatch = 'Solid' if fill == 100 else 'None' if fill == 0 \
                    else 'Hatched'
                if 0 < fill < 100:
                    import_log.log(layout_el.get('name', 'main'), 'polygon',
                                   f'{net}: fill="{fill}" -> plain Hatched; '
                                   'Altium has no hatch density')
                if el.get('clearance'):
                    import_log.log(layout_el.get('name', 'main'), 'polygon',
                                   f'{net}: per-polygon clearance '
                                   f'{el.get("clearance")} µm has no home '
                                   'until the DRC rules are exported')
                builder.add_polygon(
                    outline_vertices=_polygon_vertices(
                        el.findall('vertex'), width, dx_um, dy_um,
                        f'signal {net}'),
                    layer=_copper_layer(el.get('layer'), n_copper,
                                        f'signal {net} polygon'),
                    net=net,
                    name=name,
                    track_width_mils=_mil(width),
                    hatch_style=hatch,
                    # Pour over same-net copper instead of keeping a gap from
                    # it. In Eagle (and therefore in the IR) a pour simply
                    # MERGES with its own net's tracks — "don't pour over"
                    # has no counterpart there, and Altium's own default is
                    # the opposite. Ground truth on the value pair: every
                    # polygon of both real boards in testData carries
                    # POUROVER=TRUE / POUROVERSTYLE=1.
                    pour_over_style=1,
                    # IR rank IS Altium's pour order: the smaller number
                    # pours first and keeps the contested area, in both.
                    # Default 1, like every other path (ir_schema.md).
                    pour_index=int(el.get('rank', 1) or 1),
                )
                builder.polygons[-1].pour_over = True   # add_polygon sets
                # only the style, never the flag that turns it on
                pours.append((el, name))
                n['poly'] += 1
    return n, pours


# ---------------------------------------------------------------------------
# Design rules
#
# Altium keeps the pad-to-pour connection style NOT on the pad and NOT on the
# polygon but in a RULE: PolygonConnect, matched by two scope expressions and
# resolved by priority (a SMALLER number wins). A fresh document already
# carries a generic All/All Relief rule — the board default. The IR has one
# boolean per polygon, so the projection is: keep that generic Relief as the
# fallback, and give every polygon whose IR says thermals="0" its own
# IsNamedPolygon(...) Direct rule above it.
# ---------------------------------------------------------------------------

# Vias NEVER get a thermal relief — user's standing decision (2026-07-28),
# not a per-board fact: a relief on a via is pointless (nothing to solder,
# nothing to protect from heat sinking) and only adds resistance. Altium's
# own scope for it, ground truth from a real board where an engineer added
# the same rule by hand (testData/altium/IND/RLT504_117C.PcbDoc).
_VIA_DIRECT = {'SCOPE1EXPRESSION': 'isVia', 'SCOPE2EXPRESSION': 'All',
               'CONNECTSTYLE': 'Direct'}

# Rules6 is a BINARY stream: [2-byte leader][4-byte length][text payload], and
# the leader is the rule kind's numeric id — NOT padding. Writing zeros made
# every rule read back as kind 0 (Clearance, whose id really is 0) with alien
# fields; Altium drew garbage and then crashed. Existing records keep their
# own leader, a rule cloned from one inherits it, and a kind that has to be
# created from scratch needs its id from ground truth
# (testData/altium/IND/RLT504_117C.PcbDoc, which carries both).
_RULE_LEADER = {
    'MinimumAnnularRing':    b'\x13\x00',
    'BoardOutlineClearance': b'\x3f\x00',
    'SupplyNets':            b'\x29\x00',
}


# Body-to-body spacing: NO CONSTRAINT. Nothing in the IR states one — Eagle
# has no such check at all, so a board imported from there was never designed
# against it, and Altium's stock 10 mil lights up as violations on a board
# that is perfectly fine. 8 mil was tried first and was the wrong shape of
# answer (user, 2026-07-30): any positive number is our invention, it only
# moves the threshold, and the violations come back wherever parts sit
# tighter. Zero says what is actually true — the source states nothing.
_COMPONENT_CLEARANCE_MILS = 0


def _supply_nets(ir_root):
    """Net names that carry a POWER SYMBOL — the ones Altium calls supply.

    Altium derives a `Supply Nets` rule from the schematic for each of them
    and offers it on every ECO until the board has one too, so the board
    writes them itself. Ground truth for the record shape: the user's Base
    after a real ECO (`DEFINEDBYLOGICALDOCUMENT=TRUE`, `VOLTAGE=' 0.000'`,
    names `Schematic Supply Nets`, `_1`, `_2`, ...).
    """
    pool = {s.get('name'): s for s in ir_root.findall('symbols/symbol')}
    sup_comps = set()
    for comp_el in ir_root.findall('component'):
        for _gate, sym_name in component_gates(comp_el):
            sym_el = pool.get(sym_name)
            if sym_el is not None and any(p.get('direction') == 'sup'
                                          for p in sym_el.findall('pin')):
                sup_comps.add(comp_el.get('name'))
    out = set()

    def _scan(canvas, prefix=''):
        sup_desigs = {i.get('name') for i in canvas.findall('instance')
                      if i.get('component') in sup_comps}
        for net_el in canvas.findall('net'):
            if any(r.get('part') in sup_desigs
                   for seg in net_el.findall('segment')
                   for r in seg.findall('pinref')):
                out.add(prefix + net_el.get('name', ''))

    sch_el = ir_root.find('schematic')
    if sch_el is not None:
        _scan(sch_el)
        # A module's nets live on the board once per instance, under the
        # channel address.
        by_name = {m.get('name'): m for m in ir_root.findall('module')}
        for inst in sch_el.findall('instance'):
            mod = by_name.get(inst.get('module') or '')
            if mod is not None:
                _scan(mod, prefix=f'{inst.get("name")}:')
    return out


def _uid(seed):
    """Deterministic 8-char Altium UNIQUEID — regenerating the board must not
    churn the rule identities."""
    h = hashlib.md5(seed.encode('utf-8')).hexdigest()
    return ''.join(chr(ord('A') + int(h[i:i + 2], 16) % 26) for i in range(0, 16, 2))


def _dr_mil(um):
    """IR µm -> the dimension string Altium's rule fields carry."""
    return f'{float(um) / 25.4:.4f}mil'


def _build_rules(builder, layout_el, pour_names, supply_nets=()):
    """Rewrite the Rules6 stream: the IR's 6 DRC numbers and the polygon
    connect styles.

    pour_names: [(polygon element, name we gave it)] in emit order.
    """
    # (fields, leader, original payload) — an untouched record must go back
    # out byte-identical, which is what the payload is for.
    recs = [[dict(r.raw_record), bytes(r.record_leader or b''),
             bytes(r.raw_record_payload or b'')]
            for r in builder.rules_data.records]
    ir_rules = layout_el.find('rules')
    attrs = dict(ir_rules.attrib) if ir_rules is not None else {}

    # --- the flat 6-number core, onto each kind's GENERIC (All/All) rule
    by_attr = {attr: (kind, field) for kind, (attr, field) in DRC_RULES.items()}
    written = set()
    for entry in recs:
        rec = entry[0]
        kind = rec.get('RULEKIND')
        if kind not in DRC_RULES:
            continue
        if kind == 'Clearance' and 'OBJECTCLEARANCES' in rec:
            # The stock rule carries a per-object-pair table in which EVERY
            # "...-to-Hole" pair is 0 — i.e. copper may touch a drilled hole.
            # That is what shorted the pours onto tolmach's four mounting
            # holes (netless unplated pads, so different nets by definition).
            # Neither real board in testData carries the field at all: it is
            # a later addition, and without it the plain GAP applies to holes
            # like it does to everything else. Dropped whether or not the IR
            # supplies its own numbers — a source with no <rules> (every
            # KiCad-born project today) must not inherit the short either.
            rec.pop('OBJECTCLEARANCES')
            # A record whose fields we touched must NOT keep its original
            # bytes: AltiumPcbRule passes the raw payload straight through
            # whenever it still matches, and the edit would vanish silently.
            entry[2] = b''
        attr, field = DRC_RULES[kind]
        if attr not in attrs or rec.get('SCOPE1EXPRESSION') != 'All' \
                or rec.get('SCOPE2EXPRESSION') != 'All':
            continue
        rec[field] = _dr_mil(attrs[attr])
        entry[2] = b''
        if kind == 'Clearance':
            rec['GENERICCLEARANCE'] = rec[field]
        if kind == 'Width':
            # a Min above the stock Max would be a self-contradicting rule
            for other in ('MAXLIMIT', 'PREFEREDWIDTH'):
                if _parse_mil(rec.get(other)) < float(attrs[attr]) / 25.4:
                    rec[other] = rec[field]
        written.add(attr)

    # Two kinds a fresh document simply does not have — Altium materializes
    # them only when a user adds them (same finding as the import side).
    # Field shape copied from a real board that has them.
    for attr in sorted(set(attrs) - written):
        kind, field = by_attr.get(attr, (None, None))
        if kind is None:
            continue
        rec = {'SELECTION': 'FALSE', 'LAYER': 'UNKNOWN', 'LOCKED': 'FALSE',
               'POLYGONOUTLINE': 'FALSE', 'USERROUTED': 'TRUE',
               'KEEPOUT': 'FALSE', 'UNIONINDEX': '0', 'RULEKIND': kind,
               'NETSCOPE': 'DifferentNets' if field == 'GAP' else 'AnyNet',
               'LAYERKIND': 'SameLayer',
               'SCOPE1EXPRESSION': 'All', 'SCOPE2EXPRESSION': 'All',
               'NAME': kind, 'ENABLED': 'TRUE', 'PRIORITY': '1',
               'COMMENT': '', 'UNIQUEID': _uid(kind),
               'DEFINEDBYLOGICALDOCUMENT': 'FALSE',
               field: _dr_mil(attrs[attr])}
        if field == 'GAP':
            rec['GENERICCLEARANCE'] = rec[field]
        recs.append([rec, _RULE_LEADER[kind], b''])

    # Supply Nets: Altium builds one per power net FROM THE SCHEMATIC and
    # keeps offering it until the board carries its own. Record shape and the
    # naming (plain name first, then _1, _2 …, priorities running the other
    # way) are copied from a real ECO's output.
    for i, net in enumerate(supply_nets):
        name = 'Schematic Supply Nets' + (f'_{i}' if i else '')
        recs.append([{
            'SELECTION': 'FALSE', 'LAYER': 'UNKNOWN', 'LOCKED': 'FALSE',
            'POLYGONOUTLINE': 'FALSE', 'USERROUTED': 'TRUE',
            'KEEPOUT': 'FALSE', 'UNIONINDEX': '0', 'RULEKIND': 'SupplyNets',
            'NETSCOPE': 'AnyNet', 'LAYERKIND': 'SameLayer',
            'SCOPE1EXPRESSION': f"InNet('{net}')", 'SCOPE2EXPRESSION': 'All',
            'NAME': name, 'ENABLED': 'TRUE',
            'PRIORITY': str(len(supply_nets) - i),
            'COMMENT': '', 'UNIQUEID': _uid(name),
            'DEFINEDBYLOGICALDOCUMENT': 'TRUE', 'VOLTAGE': ' 0.000',
        }, _RULE_LEADER['SupplyNets'], b''])

    # Component clearance: the stock rule's 10 mil is Altium's, not the
    # design's (see _COMPONENT_CLEARANCE_MILS).
    for entry in recs:
        if entry[0].get('RULEKIND') == 'ComponentClearance':
            entry[0]['GAP'] = f'{_COMPONENT_CLEARANCE_MILS}mil'
            entry[0]['VERTICALGAP'] = f'{_COMPONENT_CLEARANCE_MILS}mil'
            entry[2] = b''

    _fit_rules_to_geometry(builder, recs)

    # --- PolygonConnect: via rule first, then the named Direct pours, then
    # the stock generic Relief as the weakest fallback.
    stock = next((e for e in recs if e[0].get('RULEKIND') == 'PolygonConnect'
                  and e[0].get('SCOPE2EXPRESSION') == 'All'), None)
    if stock is not None:
        template, leader = dict(stock[0]), stock[1]
        connect = [[dict(template, NAME='PolygonConnect_Via',
                         UNIQUEID=_uid('PolygonConnect_Via'), **_VIA_DIRECT),
                    leader, b'']]
        for el, name in pour_names:
            if el.get('thermals') != '0':
                continue
            connect.append([dict(
                template, NAME=f'PolygonConnect_{name}',
                UNIQUEID=_uid(f'PolygonConnect_{name}'),
                SCOPE1EXPRESSION='All',
                SCOPE2EXPRESSION=f"IsNamedPolygon('{name}')",
                CONNECTSTYLE='Direct'), leader, b''])
        connect.append(stock)
        for i, entry in enumerate(connect, start=1):
            entry[0]['PRIORITY'] = str(i)
            entry[2] = b''
        recs = [e for e in recs if e[0].get('RULEKIND') != 'PolygonConnect']
        recs.extend(connect)

    builder.rules_data = PcbDocRulesData(records=tuple(
        AltiumPcbRule.from_record(rec, index=i, record_leader=leader,
                                  record_payload=payload)
        for i, (rec, leader, payload) in enumerate(recs)))
    return len(recs), sum(1 for e in recs
                          if e[0].get('RULEKIND') == 'PolygonConnect')


def _fit_rules_to_geometry(builder, recs):
    """Make the brackets tell the truth about the board we just wrote.

    A stock document ships rules that describe an imaginary board: routing
    vias exactly 50 mil across, no track over 10 mil, no hole over 100 mil.
    Ship those next to tolmach's geometry and Altium is right to scream —
    54 vias, 89 tracks and 4 mounting holes violate rules we put there
    ourselves. That is our defect, not a finding about the design.

    The split is deliberate: a MINIMUM is design intent and comes from the
    IR <rules> (a track thinner than the declared minimum is a REAL
    violation and must stay visible), while a MAXIMUM is only a bracket —
    it must not contradict what exists. Via style has no IR counterpart at
    all, so it is taken wholly from the vias actually placed.
    """
    def _stat(values):
        return (min(values), max(values),
                max(set(values), key=values.count))       # min, max, commonest

    widths = [round(t.width_mils, 4) for t in builder.tracks
              if 1 <= int(t.layer) <= 32] +              [round(a.width_mils, 4) for a in builder.arcs
              if 1 <= int(a.layer) <= 32]
    holes = [round(p.hole_size_mils, 4) for p in builder.pads
             if p.hole_size_mils > 0]
    via_d = [round(v.diameter_mils, 4) for v in builder.vias]
    via_h = [round(v.hole_size_mils, 4) for v in builder.vias]

    for rec, _leader, _payload in recs:
        kind = rec.get('RULEKIND')
        if kind == 'Width' and widths:
            lo, hi, common = _stat(widths)
            floor = _parse_mil(rec.get('MINLIMIT'))
            rec['MAXLIMIT'] = f'{max(hi, floor):.4f}mil'
            rec['PREFEREDWIDTH'] = f'{min(max(common, floor), max(hi, floor)):.4f}mil'
        elif kind == 'HoleSize' and holes:
            floor = _parse_mil(rec.get('MINLIMIT'))
            rec['MAXLIMIT'] = f'{max(max(holes), floor):.4f}mil'
        elif kind == 'RoutingVias' and via_d:
            lo, hi, common = _stat(via_d)
            hlo, hhi, hcommon = _stat(via_h)
            rec.update({'MINWIDTH': f'{lo:.4f}mil', 'MAXWIDTH': f'{hi:.4f}mil',
                        'WIDTH': f'{common:.4f}mil',
                        'MINHOLEWIDTH': f'{hlo:.4f}mil',
                        'MAXHOLEWIDTH': f'{hhi:.4f}mil',
                        'HOLEWIDTH': f'{hcommon:.4f}mil'})
        else:
            continue
        _payload_cleared = True
    # a touched record must not keep its original bytes (see _build_rules)
    for entry in recs:
        if entry[0].get('RULEKIND') in ('Width', 'HoleSize', 'RoutingVias'):
            entry[2] = b''


def _parse_mil(s):
    try:
        return float(str(s).replace('mil', '').strip())
    except (TypeError, ValueError):
        return float('inf')


# ---------------------------------------------------------------------------
# Deferred content — counted and logged, never silently dropped
# ---------------------------------------------------------------------------

_STAGE1_TAGS = ('element', 'rules', 'passthrough', 'text', 'signal', 'hole')


# Tags that are not going to Altium at all, and why. Kept OUT of the deferred
# list on purpose: "deferred" promises a later stage, and promising one for
# something nobody intends to build is just a lie that nags every run.
_NOT_EXPORTED = {
    'attr': 'board attributes stay in the IR — the ones in play (NOABS_*) '
            'are settings for Eagle-side 3D tooling, not facts about the board',
}


def _log_deferred(layout_el, loop_ids):
    counts = {}
    for el in layout_el:
        if el.tag in _STAGE1_TAGS or id(el) in loop_ids:
            continue
        counts[el.tag] = counts.get(el.tag, 0) + 1
    name = layout_el.get('name', 'main')
    for tag in sorted(counts):
        why = _NOT_EXPORTED.get(tag)
        import_log.log(name, tag,
                       f'BOARD_EXPORT: {counts[tag]} <{tag}> ' +
                       (f'not exported — {why}' if why
                        else 'deferred to a later stage'))
    return {t: n for t, n in counts.items() if t not in _NOT_EXPORTED}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def export_board(ir_root, out_dir, proj_name, pcblib_path, sch_link=None,
                 hier=None, desig_of=None, net_names=None):
    """One IR <layout> -> <proj_name>.PcbDoc in out_dir. Returns the path, or
    None when the project carries no layout.

    sch_link: {element address: (unique_id, lib_reference, library_name)}
    taken from the components the SchDoc export actually placed — what makes
    Altium treat the two documents as the SAME design rather than two
    unrelated files.
    hier: {room name: sheet symbol unique id} — a part on a channel is
    identified by a PATH, not by its own id (BC2087 ground truth:
    SOURCEUNIQUEID `\\<sheet symbol>\\<component>`, SOURCEHIERARCHICALPATH
    `TopLevel\\<room>`); with ONE shared child sheet the component's own id is
    the same in every channel, and the sheet symbol's id is what separates
    them.
    desig_of: {element address: designator} — on the Altium path a channel
    part is named by the project's channel format
    (altium_exporter.channel_designator), not by the IR's Eagle-style
    flattening. Absent (standalone board export) the IR spelling stands.
    net_names: {IR signal name: name to write} — same story for the nets of a
    channel, taken from the compiler rather than guessed
    (altium_project_exporter._compiled_net_names)."""
    layouts = ir_root.findall('layout')
    if not layouts:
        return None
    if len(layouts) > 1:
        raise ValueError(f'{proj_name}: {len(layouts)} <layout>s — board '
                         'export v1 handles exactly one; name the board to '
                         'export explicitly when this becomes real')
    layout_el = layouts[0]
    layout_name = layout_el.get('name', 'main')
    sch_link = sch_link or {}
    hier = hier or {}
    desig_of = desig_of or {}

    out_dir = Path(out_dir)
    pcblib_path = Path(pcblib_path)
    lib = AltiumPcbLib.from_file(str(pcblib_path))
    fp_by_name = {fp.name: fp for fp in lib.footprints}

    comp_by_name = {c.get('name'): c for c in ir_root.findall('component')}
    # Footprints the LAYOUT owns: a board-only padless element (Eagle logo)
    # has no component to hold its footprint.
    local_fp = {f.get('name'): f for f in layout_el.findall('footprint')}
    # An element addresses either a top-level instance or a part inside a
    # module instance ('INST:REFDES'); both the designator written here and
    # the one the SchDoc export wrote come from the same rule.
    resolve_element = designator_resolver(ir_root)
    # Nets keep their IR spelling unless the caller hands over the compiler's
    # names — a board exported on its own has no project to compile.
    _renames = net_names or {}
    net_names = lambda n: _renames.get(n, n)

    # --- outline first: it fixes the translation everything else uses
    segs = _outline_segments(layout_el)
    loop = _chain(segs)
    min_x, min_y, _, _ = _outline_bbox(loop)
    dx_um = -min_x + _MARGIN_MILS * 25.4
    dy_um = -min_y + _MARGIN_MILS * 25.4

    builder = PcbDocBuilder()
    # Same plan, same input, same slots as the PcbLib the footprints come
    # from — a mechanical layer must mean the same thing in both files.
    layer_plan, layer_pairs = altium_layers.plan(altium_layers.layers_used(ir_root))
    altium_layers.declare(builder, layer_plan, layer_pairs)
    builder.set_board_outline(
        AltiumBoardOutline(vertices=_outline_vertices(loop, dx_um, dy_um)))
    builder.set_origin_mils(_MARGIN_MILS, _MARGIN_MILS)
    builder.set_layer_stack_template(
        _stack_template(layout_el.get('stack'), layout_name))

    # --- nets BEFORE the components: a placed pad binds to its net by name
    for sig in layout_el.findall('signal'):
        builder.add_net(net_names(sig.get('name')))
    pad_nets = _pad_nets(layout_el, net_names)

    # --- components
    n_bottom = n_moved = n_hidden = 0
    for el in layout_el.findall('element'):
        address = el.get('name')
        inst, desig = resolve_element(address)
        desig = desig_of.get(address, desig)
        if inst is None:
            # A BOARD-ONLY element (Eagle artwork/logo: padless, no part in
            # the schematic) carries its footprint in the LAYOUT itself.
            # Altium places it as a PCB-only component — no ECO link, which
            # is exactly what it is.
            fp_el = local_fp.get(el.get('footprint'))
            if fp_el is None:
                raise ValueError(
                    f'layout element {address!r}: no <instance> with that '
                    'designator and no layout-local <footprint '
                    f'name="{el.get("footprint")}"> — board and schematic '
                    'disagree')
            comp_el = None
        else:
            comp_el = comp_by_name.get(inst.get('component'))
            if comp_el is None:
                raise ValueError(f'element {desig!r}: component '
                                 f'{inst.get("component")!r} not in the pool')
            fp_el = instance_footprint(comp_el, inst)
            if fp_el is None:
                raise ValueError(f'element {desig!r}: component '
                                 f'{comp_el.get("name")!r} has no footprint')
        fp = fp_by_name.get(fp_name(fp_el))
        if fp is None:
            raise ValueError(f'element {desig!r}: footprint '
                             f'{fp_el.get("name")!r} is not in '
                             f'{pcblib_path.name}')

        bottom = el.get('side') == 'bottom'
        ir_rot = float(el.get('rot', 0) or 0) % 360
        # see the module docstring: IR mirrors about Y, Altium about X
        rot = (ir_rot + 180) % 360 if bottom else ir_rot
        if bottom:
            n_bottom += 1
        value = ('' if comp_el is None
                 else resolved_attrs(comp_el, inst, fp_el).get('value', ''))
        position_mils = (_mil(float(el.get('x', 0)) + dx_um),
                         _mil(float(el.get('y', 0)) + dy_um))

        idx = builder.place_footprint(
            fp,
            designator=desig,
            position_mils=position_mils,
            layer='BOTTOM' if bottom else 'TOP',
            rotation_degrees=rot,
            source_footprint_library=pcblib_path.name,
            comment_text=value or None,
            comment_visible=False,
            source_pcblib=lib,
            pad_nets=pad_nets.get(address),   # <contactref> speaks addresses
        )
        _fix_placed_fills(builder, idx, fp, position_mils, rot, bottom)
        n_moved += _apply_text_overrides(builder, idx, el, fp_el,
                                         position_mils, rot, bottom)
        n_hidden += _apply_text_visibility(builder, idx, el, fp_el)

        # --- the ECO link. place_footprint has no parameter for it (only the
        # geometry-less add_component has), so the fields are written onto the
        # component record the same way altium_monkey's own
        # set_component_description does — raw_record IS the serialized form.
        uid, lib_ref, lib_name = sch_link.get(address, ('', '', ''))
        rec = builder.components[idx].raw_record
        rec['SOURCEDESIGNATOR'] = desig
        if uid:
            # A part on a child sheet is identified by its PATH from the top:
            # `\<sheet symbol uid>\<component uid>`, and its room is named in
            # SOURCEHIERARCHICALPATH — BC2087 ground truth. Top-level parts
            # keep the single-segment form Altium writes for them.
            room = address.split(':', 1)[0] if ':' in address else None
            ss_uid = hier.get(room)
            path = [uid] if ss_uid is None else [ss_uid, uid]
            rec['SOURCEUNIQUEID'] = ''.join('\\' + p for p in path)
            rec['SOURCEHIERARCHICALPATH'] = ('TopLevel' if room is None
                                             else f'TopLevel\\{room}')
        else:
            # Eagle lets an element live on the board alone; Altium's
            # synchronization model does not, so every ECO will offer to
            # remove it. The project-wide cure (ECO Generation -> Remove
            # Components -> Ignore Differences) is worse than the disease: it
            # silences EVERY component removal, so a part deleted from the
            # schematic would silently stay on the board. Left to the user to
            # untick — user decision 2026-07-30.
            import_log.log(desig, '', 'PCB-ONLY element (no part in the '
                           'schematic): Altium has no such concept, so every '
                           '"Update PCB" will offer to remove it. Untick that '
                           'line. Do NOT set ECO Generation -> Remove '
                           'Components -> Ignore Differences to hide it: that '
                           'switch also hides REAL deletions')
        if lib_ref:
            rec['SOURCELIBREFERENCE'] = lib_ref
        if lib_name:
            rec['SOURCECOMPONENTLIBRARY'] = lib_name

    outline_ids = {id(s) for s in layout_el
                   if s.tag in ('line', 'arc')
                   and s.get('layer') == str(LAYER_DIMENSION)}
    drawn_ids, n_gfx = _emit_free_graphics(builder, layout_el, dx_um, dy_um,
                                           layer_plan, outline_ids)
    n_free = _emit_free_texts(builder, layout_el, dx_um, dy_um, layer_plan)
    n_holes = _emit_holes(builder, layout_el, dx_um, dy_um)
    n_cu, pours = _emit_copper(builder, layout_el, dx_um, dy_um,
                               len(parse_stack(layout_el.get('stack'))[0]),
                               net_names)
    # Only nets the BOARD actually has: a supply net whose parts are all off
    # this layout would give Altium a rule scoped to nothing.
    board_nets = {net_names(s.get('name'))
                  for s in layout_el.findall('signal')}
    n_rules, n_connect = _build_rules(
        builder, layout_el, pours,
        supply_nets=sorted(net_names(n) for n in _supply_nets(ir_root)
                           if net_names(n) in board_nets))

    counts = _log_deferred(layout_el, outline_ids | drawn_ids)

    _clear_pour_membership(builder)

    path = out_dir / f'{proj_name}.PcbDoc'
    builder.save(path)
    print(f'Written: {path}')
    print(f'  outline {len(loop)} segment(s), '
          f'{len(layout_el.findall("element"))} components '
          f'({n_bottom} on the bottom side), '
          f'stack {layout_el.get("stack")}')
    print(f'  {n_moved} per-instance text override(s), {n_hidden} '
          f'designator/comment text(s) hidden (no placeholder in the '
          f'footprint), '
          f'{n_free} board-level text(s), {n_gfx} board-level graphic(s), '
          f'{n_holes} mounting hole(s)')
    print(f'  {len(layout_el.findall("signal"))} nets: {n_cu["track"]} track(s), '
          f'{n_cu["arc"]} arc(s), {n_cu["via"]} via(s), {n_cu["poly"]} pour(s)')
    print(f'  {n_rules} design rules ({n_connect} PolygonConnect)')
    if counts:
        print('  deferred to a later stage: '
              + ', '.join(f'{k} x{v}' for k, v in sorted(counts.items())))
    return path
