"""KiCad schematic CANVAS (.kicad_sch wires/junctions/labels/placed symbols)
-> IR <schematic> (instances + <net>).

Connectivity rules here are not derived from the IR's own "junction/pinref
stored explicitly" principle (decisions.md) — they're empirically reverse-
engineered from how KiCad itself decides what's electrically connected, since
that's what we're importing FROM. See decisions.md "KiCad-специфика: когда
исходный файл реально требует junction для связности" for the full
derivation and the synthetic fixture (testData/junction/junction.kicad_sch)
this was checked against.

Summary of the rule implemented by _build_nets:
  - Two wires sharing a coordinate as BOTH their own true endpoint are
    connected unconditionally (any number of wires, junction or not).
  - A pin is connected to a wire whenever the wire has a true ENDPOINT at
    the pin's position — pins never need a junction (KiCad: "the junction
    rule is true for wires, not for pins").
  - Anything else touching a wire's BODY (not at that wire's own endpoint —
    a real crossing, or another wire's/pin's endpoint landing mid-segment)
    is connected only if an explicit `(junction ...)` sits at that exact
    point. Confirmed experimentally (not just from geometry): KiCad's editor
    auto-places a junction for the endpoint-on-body case, but deleting it
    afterwards genuinely splits the net — the junction element is the real
    signal, not a cosmetic dot.
"""
import math
from collections import defaultdict

_UM_PER_MM = 1000


def _pt(x_mm, y_mm):
    """mm (float) -> (x_um, y_um) int tuple — exact-equality key for the
    union-find below. KiCad coordinates are always a clean few decimal
    places (0.01 mm grid or finer multiples of 0.254/1.27/...), so rounding
    to the nearest µm never collides two genuinely distinct points.
    """
    return (round(x_mm * _UM_PER_MM), round(y_mm * _UM_PER_MM))


class _DSU:
    """Plain union-find keyed by arbitrary hashable points. No union-by-rank/
    path compression — schematic point counts are small (low thousands at
    most), not worth the extra code.
    """

    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _collinear_between(p1, p2, q):
    """True if integer point q lies ON the segment p1-p2 (collinear, within
    bounds), INCLUDING the endpoints themselves — callers exclude the
    endpoint case separately where that distinction matters.
    """
    (x1, y1), (x2, y2), (x, y) = p1, p2, q
    cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
    if cross != 0:
        return False
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


def _mirror_and_angle(angle, mirror):
    """KiCad placement (angle degrees, mirror axis 'x'/'y'/None) -> the pair
    needed downstream:
      - (eff_angle, flip_x): feed _abs_pin_pos_mm to get a pin's absolute
        position in KiCad's own Y-down sheet space.
      - (ir_rot, ir_mirror): what to store on the IR <instance> itself.

    Both reduce a two-axis mirror to a SINGLE convention (mirror-Y-style,
    i.e. negate local X) via the standard O(2) identity Rotate(θ)·MirrorX =
    Rotate(θ+180)·MirrorY — so 'mirror x' is just 'mirror y' at angle+180.

    ir_rot = eff_angle, ir_mirror = flip_x — LITERAL copy, no compensation
    for the Y-flip between KiCad's sheet space and IR's canvas. Confirmed
    directly by the user against real KiCad and real Eagle side by side,
    for BOTH body orientation AND pin/wire connectivity simultaneously
    (U6, KiCad angle=90 — setting Eagle's own `rot="R90"` by hand, not
    any derived value, makes the body AND every wire land correctly).

    This directly contradicts two previous attempts at a "compensating"
    formula (ir_rot=eff_angle+180 with ir_mirror flipped, then
    ir_rot=-eff_angle with ir_mirror unchanged) — both were "confirmed" by
    a same-file pin↔wire cross-check that turned out to be structurally
    incapable of catching a UNIFORM angle error: that check recomputed a
    pin's expected position from the SAME exported .sch's own `<instance
    rot=...>` and `<symbol><pin>` geometry, so it only verifies INTERNAL
    consistency between the exporter's instance-rotation code path and its
    pin-position code path — both driven by the same ir_rot value, so a
    systematic offset applied identically to both cancels out and the
    check passes regardless of whether ir_rot itself is correct. It can
    catch a pin-vs-pin MISMATCH within one instance (which is how the
    earlier attempts' real bugs were found) but never a whole-symbol
    rotation error shared by everything in the file. Only a comparison
    against an INDEPENDENT source of truth (real KiCad on screen, checked
    by the user) can catch that class of bug — see decisions.md "KiCad:
    ir_rot/ir_mirror — литеральное копирование, не формула" for the full
    history and the methodological lesson.
    """
    ir_rot = (angle + 180) % 360 if mirror == 'x' else angle % 360
    flip_x = mirror in ('x', 'y')
    # The STORED angle stays the literal copy (confirmed by the user against
    # real KiCad + real Eagle side by side, U6 — see below). The angle fed
    # into THIS module's own pin-position math, however, must be NEGATED for
    # a mirrored placement: _abs_pin_pos_mm composes flip-then-rotate in its
    # Y-mixed working convention, and KiCad's actual mirrored rendering
    # corresponds to the OPPOSITE rotation sense there. Proven empirically
    # twice over: (a) tolmach closed loop — un-negated eff swapped/lost pins
    # on every mirrored rot-90/270 instance (R47/C48/Q1/ZD2), negated form
    # reproduces all 32/32 source nets; (b) kicad-cli's own netlist of the
    # SAME literal-angle file agrees with the source IR, so the file angle
    # itself was never wrong — only this conversion. Negating the stored
    # ir_rot as well (first fix attempt) broke the loop the other way:
    # mirrored instances came back rot 90 -> 270 and every smashed-field
    # record landed 180° off.
    eff_angle = (-ir_rot) % 360 if flip_x else ir_rot
    ir_mirror = 1 if flip_x else 0
    return eff_angle, flip_x, ir_rot, ir_mirror


def field_to_kicad(inst_x_um, inst_y_um, ir_rot, ir_mirror,
                   lx_um, ly_um, lrot):
    """THE field transform, forward: an instance-local field record (IR Y-up
    µm anchor + local angle — a library placeholder anchor or a smashed
    <instance><text> override, same thing) -> absolute IR-canvas anchor +
    the folded absolute angle KiCad displays.

    Single source of truth — the exporter's default and override branches
    and the importer's inverse all call this pair; the transform used to
    live in three places and every fix in one broke another (user: «починим
    раз и навсегда»).

    Rules (each one empirically pinned):
      - position: flip local X when mirrored, rotate by −θ when mirrored
        else +θ (six hand-placed IRLML9301 in real KiCad, rot 0/90/180/270
        x mirror none/x/y — decisions.md «Баг 1»);
      - angle: emit the field's LOCAL angle only — KiCad STORES the field
        angle relative to the symbol body and adds the symbol's own
        rotation/mirror at render time (NOT us). Proven decisively by
        rendering RC through kicad-cli: a rot-90 C's fields land VERTICAL
        (correct, matches Eagle) only when the property angle is the local
        0 — property angle 90 renders them horizontal (90+90=180). The
        earlier "field rotates with the symbol" reading double-counted the
        rotation. Cross-checked against a KiCad-owned resave (step4): KiCad
        itself writes property angle 0 for every rot-90 symbol. Folded to
        [0,180), justify preserved (kicad_exporter._norm_text_angle).
    """
    fx = -float(lx_um) if ir_mirror else float(lx_um)
    th = math.radians(-ir_rot if ir_mirror else ir_rot)
    ax = inst_x_um + fx * math.cos(th) - float(ly_um) * math.sin(th)
    ay = inst_y_um + fx * math.sin(th) + float(ly_um) * math.cos(th)
    return ax, ay, lrot % 180


def field_from_kicad(inst_x_um, inst_y_um, ir_rot, ir_mirror,
                     ax_um, ay_um, arot):
    """THE field transform, inverse of field_to_kicad: absolute KiCad
    anchor/angle -> instance-local record. field_from_kicad(field_to_kicad)
    is the identity modulo the [0,180) angle fold (which both formats
    render identically, justify preserved)."""
    ddx, ddy = ax_um - inst_x_um, ay_um - inst_y_um
    th = math.radians(ir_rot if ir_mirror else -ir_rot)
    lx = ddx * math.cos(th) - ddy * math.sin(th)
    ly = ddx * math.sin(th) + ddy * math.cos(th)
    if ir_mirror:
        lx = -lx
    # KiCad's stored field angle IS already the local (symbol-relative)
    # angle — pass it straight through (see field_to_kicad).
    return lx, ly, arot % 180


def _abs_pin_pos_mm(pin_x_mm, pin_y_mm, inst_x_mm, inst_y_mm, eff_angle, flip_x):
    """Pin local position (mm, Y-up library convention) + this instance's
    ALREADY-Y-flipped IR-canvas position (inst_x_mm, inst_y_mm — i.e. the
    same (x, -sym.position.Y) pair that ends up on `<instance x=... y=...>`)
    -> absolute pin position, in that SAME IR Y-up space. Caller writes the
    result straight into `<net><line>`/`<pinref>` coordinates, no further
    sign flip needed — this function's output IS already IR-canonical.

    Plain rotate(eff_angle) + optional X-mirror, matching EXACTLY the
    canonical IR transform svg_renderer.py's `_inst_point` and Eagle's own
    `<instance rot=...>` placement use — no Y-flip of the pin itself, no
    Y-flip of the instance position inside this function. Both used to be
    done (this function took RAW KiCad Y-down inst_y and returned RAW
    KiCad Y-down output, flipped ONCE more by the caller at XML-write
    time) — that additional round-trip through KiCad-native space was
    itself a hidden compensation, symmetric to the one already removed
    from `_mirror_and_angle` (see decisions.md "KiCad: ir_rot/ir_mirror —
    литеральное копирование, не формула"), and just as wrong for the same
    reason: found on U6 (`SN74LVC1T45DBV`, rot=90) — pins "A"/"B" sit at
    asymmetric local positions ((-10.16,0)/(10.16,0)), so the old
    round-trip-through-KiCad-space formula placed pin "B" at a DIFFERENT
    point than the one Eagle's own `<instance rot="R90">` transform puts
    it at, confirmed against real KiCad which wire pin 4 (B) actually
    connects to. Removing BOTH round-trips (this one and the instance-
    rotation one) — not just one — is what actually matches Eagle.
    wires/junctions/labels are unaffected: they were never routed through
    this function, they're plain KiCad-native points flipped once at
    write time, same as always.
    """
    x, y = pin_x_mm, pin_y_mm
    if flip_x:
        x = -x
    theta = math.radians(eff_angle)
    xr = x * math.cos(theta) - y * math.sin(theta)
    yr = x * math.sin(theta) + y * math.cos(theta)
    return inst_x_mm + xr, inst_y_mm + yr


def _build_nets(wires, junction_points, pin_points, label_points, label_origin):
    """Compute electrical connectivity and group it into named nets.

    Args (all coordinates already (x_um, y_um) int tuples, see _pt):
      wires: [(p1, p2), ...] — one entry per drawn wire segment.
      junction_points: set of points where the source has an explicit
        `(junction ...)` marker.
      pin_points: [(point, designator, pin_name, is_sup), ...] — every
        placed component pin's absolute position. `is_sup` marks a
        Supply-symbol pin (ir_schema.md "Supply symbol") — its OWN name
        acts like an attached label (see net-naming below).
      label_points: [(point, text), ...].
      label_origin: {(point, text): human-readable "sheet @ (x,y)" string}
        — used ONLY to point at the exact source location if this island
        turns out to have conflicting label names (see below), never for
        connectivity/naming itself.

    Returns a list of nets: [{'name': str, 'segments': [seg, ...]}, ...],
    where each seg is {'wires': [(p1,p2),...], 'pinrefs': [(designator,
    pin_name),...], 'junctions': [point,...], 'labels': [(point,text),...]}
    — one seg per electrically-connected island (== one <segment>).

    Islands with no wire and at most one pinref are dropped (a lone
    unconnected pin has nothing to say) — see module docstring for why
    nothing else needs filtering.

    Hard-fails if any one electrical island carries two labels with
    DIFFERENT text — KiCad itself allows this (its own ERC picks a winner
    via "Connection Name" resolution rules that aren't stored in the file
    at all — see decisions.md "KiCad: конфликтующие ярлыки на одной цепи —
    жёсткий отказ" for why guessing at those rules isn't attempted here).
    Per the user: this is bad practice worth refusing outright, not
    silently picking one name — every occurrence across the whole project
    is collected first so the error can point at all of them at once.
    """
    dsu = _DSU()
    for p1, p2 in wires:
        dsu.union(p1, p2)

    # Every point worth checking as a potential mid-segment touch: wire
    # endpoints, pin positions, label positions, junction positions. Pins/
    # labels never need their OWN union call (see module docstring) — they
    # only need to exist as dict keys, which dsu.find() does lazily — but
    # they still need to be in this pool so a wire whose BODY happens to
    # pass through one of them is correctly evaluated against the junction
    # rule below, rather than silently ignored.
    all_points = set()
    for p1, p2 in wires:
        all_points.add(p1)
        all_points.add(p2)
    for p, *_ in pin_points:
        all_points.add(p)
    for p, _ in label_points:
        all_points.add(p)
    all_points |= junction_points

    # A LABEL touching a wire's BODY (not just its endpoint) needs no
    # junction — unlike a second conductor (another wire, a pin) touching
    # mid-segment, a label isn't claiming a new electrical bridge, it's just
    # naming whatever's already there, so there's no ambiguity to gate.
    # Confirmed real (testData/phil: "NRST" sits on the open middle of a
    # wire whose ENDS run from the MCU pin to a junction further on — no
    # junction at the label's own point at all; treating it like a wire/pin
    # touch silently dropped the name).
    label_point_set = {p for p, _ in label_points}

    for p1, p2 in wires:
        for q in all_points:
            if q == p1 or q == p2:
                continue
            if not _collinear_between(p1, p2, q):
                continue
            if q in label_point_set or q in junction_points:
                dsu.union(q, p1)

    segments = defaultdict(lambda: {'wires': [], 'pinrefs': [], 'junctions': [], 'labels': []})

    for p1, p2 in wires:
        segments[dsu.find(p1)]['wires'].append((p1, p2))
    for p, designator, pin_name, is_sup in pin_points:
        segments[dsu.find(p)]['pinrefs'].append((designator, pin_name, is_sup))
    for p in junction_points:
        if p in dsu.parent:
            segments[dsu.find(p)]['junctions'].append(p)
    for p, text in label_points:
        segments[dsu.find(p)]['labels'].append((p, text))

    by_name = defaultdict(list)
    # Names already claimed by a REAL source (label text / supply pin) —
    # the auto-namer must never generate one of these: a schematic can
    # legitimately carry a label literally named "N$2" (our own KiCad
    # exporter materializes such labels to keep net-class assignments),
    # and handing the same name to an unrelated unnamed island silently
    # merges the two by the by_name grouping below (found on the
    # user-edited multichannel round-trip).
    taken = {text for _, text in label_points}
    taken |= {pin_name for _, _, pin_name, is_sup in pin_points if is_sup}
    auto_n = 0
    conflicts = []   # collected across ALL islands, reported together at the end
    for seg in segments.values():
        if not seg['wires']:
            if not seg['pinrefs']:
                # Nothing electrical at all — just a label sitting in
                # isolation, touching neither pin nor wire. Confirmed real
                # (testData/phil: two separate "SWDIO" labels exist purely
                # as documentation/duplicate annotations, not attached to
                # the actual SWDIO wire run at all). No pin/wire content to
                # justify a <net>.
                continue
            if len(seg['pinrefs']) == 1 and not seg['labels']:
                # A lone, unlabeled, unconnected pin — nothing to say.
                # Confirmed real (testData/phil: ~26 unused MCU GPIOs and a
                # few other spare pins on U1/U2/J2, each correctly isolated
                # by the rest of the algorithm, just not worth a <net>). A
                # single pin with a label glued directly onto it instead
                # (no stub wire at all — common on MCU symbols, e.g.
                # "VREF-") still has a real name and survives.
                continue

        # name -> sorted set of "where this name came from" strings, so the
        # error below can point at every occurrence, not just say "you have
        # 2 names" and leave the user hunting for both of them.
        name_sources = defaultdict(set)
        for p, text in seg['labels']:
            name_sources[text].add(label_origin.get((p, text), text))
        for designator, pin_name, is_sup in seg['pinrefs']:
            if is_sup:
                name_sources[pin_name].add(f'{designator}.{pin_name} (supply pin)')

        if len(name_sources) > 1:
            conflicts.append(name_sources)
            continue   # naming is meaningless once ambiguous — skip net-name resolution entirely

        if name_sources:
            name = next(iter(name_sources))
        else:
            while True:
                auto_n += 1
                name = f'N${auto_n}'
                if name not in taken:
                    break

        by_name[name].append({
            'wires': seg['wires'],
            'pinrefs': [(d, p) for d, p, _ in seg['pinrefs']],
            'junctions': seg['junctions'],
            'labels': seg['labels'],
        })

    if conflicts:
        lines = []
        for name_sources in conflicts:
            for name, sources in sorted(name_sources.items()):
                for src in sorted(sources):
                    lines.append(f'  "{name}" at {src}')
        raise ValueError(
            f'{len(conflicts)} electrical net(s) carry two or more labels with DIFFERENT '
            f'names on the same connected island — this is not a real net name (Eagle-style '
            f'IR has exactly one name per net, no KiCad-style "Connection Name" resolution: '
            f'decisions.md "KiCad: конфликтующие ярлыки на одной цепи — жёсткий отказ"). Pick '
            f'one name and delete/rename the rest, then re-export:\n' + '\n'.join(lines))

    return [{'name': name, 'segments': segs} for name, segs in by_name.items()]
