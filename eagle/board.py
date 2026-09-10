"""Eagle `<board>` (`.brd`) -> IR `Layout` — conversion-eagle.md #плата (the
"Из Eagle" pass). Board space reuses the same layer table as footprints
(geometry.py's `footprint_layer` — layer-model.md: "Слой в корпусе — тот же
самый"), extended here with the one thing a pooled footprint can never
know: which of Eagle's up to 16 copper positions this particular board
actually uses, read from `<layers active=...>` and turned into the stack
formula via `ir.stack.copper_layer_numbers`.
"""

from __future__ import annotations

import re

from ir.component import Component, Device, Gate
from ir.component_instance import ComponentInstance
from ir.contactref import ContactRef
from ir.element import Element, Side
from ir.graphics import Arc, Line, Polygon, Text
from ir.layout import Layout
from ir.pad import Hole
from ir.part import Part
from ir.plating import Plating, PlatingArc, PlatingLine
from ir.rules import Rules
from ir.signal import Signal
from ir.stack import copper_layer_numbers
from ir.symbol import Symbol
from ir.units import Layer
from ir.via import Via

from . import geometry as geo
from .schematic import _ALIGN_MAP

_dru_length = geo.dru_length   # один разбор длины на весь Eagle-путь


# --------------------------------------------------------------- designrules

_CLEARANCE_KEYS = ["mdWireWire", "mdWirePad", "mdWireVia", "mdPadPad", "mdPadVia",
                    "mdViaVia", "mdSmdPad", "mdSmdVia", "mdSmdSmd"]
"""Eagle's clearance matrix — and `mdViaViaSameLayer` is deliberately NOT in
it.

rules.md keeps one clearance, so the matrix collapses to its largest member
(conversion-eagle.md: "Импорт расходящихся пар берёт максимум") — the safe
direction, since nothing Eagle rejected then passes. `mdViaViaSameLayer`
breaks that reasoning because **nobody ever sets it**: it reads 6 mil = 152
µm on all six test boards, Eagle's own untouched default, while the matrix
beside it is authored and varies (150, 152, 200). Folding the default into
the max therefore raises the floor to `max(the author's rule, 152)` and
tightens every board whose rule is finer than 6 mil.

Measured on `staya`, whose author set the matrix to 0.15 mm: of 379
clearance violations KiCad reported, **244 sat between 0.150 and 0.152 mm**
— spacings Eagle itself passes. The board is clean in Eagle and was not
in KiCad, and that was our arithmetic, not the board's."""


def _dru_params(designrules_el) -> dict[str, str]:
    if designrules_el is None:
        return {}
    return {p.get("name"): p.get("value") for p in designrules_el.findall("param")}


def convert_rules(designrules_el, log) -> Rules | None:
    """rules.md: Eagle spells one clearance as a pairwise matrix
    (wire/wire, wire/pad, pad/via, ...) and one annular ring as several
    per-kind minimums (pad top/inner/bottom, via outer/inner) — IR keeps a
    single floor for each, so the conversion takes the largest of each
    group (conversion-eagle.md: "Импорт расходящихся пар берёт максимум")."""
    params = _dru_params(designrules_el)
    if not params:
        return None

    def get_len(key: str) -> int | None:
        v = params.get(key)
        return _dru_length(v) if v is not None else None

    clearance_vals = [get_len(k) for k in _CLEARANCE_KEYS if k in params]
    clearance = max(clearance_vals) if clearance_vals else None
    if clearance_vals and len(set(clearance_vals)) > 1:
        log("design rules: clearance is a pairwise matrix in Eagle ("
            + ", ".join(f"{k}={params[k]}" for k in _CLEARANCE_KEYS if k in params)
            + f") — using the largest, {clearance} um")

    annular_keys = sorted(k for k in params if k.startswith("rlMin"))
    annular_vals = [get_len(k) for k in annular_keys]
    min_annular = max(annular_vals) if annular_vals else None
    if annular_vals and len(set(annular_vals)) > 1:
        log("design rules: minimum annular ring differs by pad/via kind in Eagle ("
            + ", ".join(f"{k}={params[k]}" for k in annular_keys)
            + f") — using the largest, {min_annular} um")

    return Rules(
        clearance=clearance,
        edge_clearance=get_len("mdCopperDimension"),
        min_width=get_len("msWidth"),
        min_drill=get_len("msDrill"),
        min_annular=min_annular,
        min_drill_web=get_len("mdDrill"),
    )


def convert_expansions(designrules_el, log) -> tuple[int, int]:
    """layout.md #маска-и-паста: IR keeps one flat expansion per board, but
    Eagle's is pad-size-dependent (a ratio clamped between a min and max).
    Where min==max the clamp forces one constant anyway, so the conversion
    is exact; otherwise the floor is kept and the approximation logged."""
    params = _dru_params(designrules_el)

    def frame(min_key: str, max_key: str, default: int) -> int:
        if min_key not in params or max_key not in params:
            return default
        lo, hi = _dru_length(params[min_key]), _dru_length(params[max_key])
        if lo != hi:
            log(f"design rules: {min_key}/{max_key} are pad-size-dependent in Eagle "
                f"({params[min_key]}..{params[max_key]}) — IR has one flat number per board, "
                f"using the floor {lo} um")
        return lo

    mask = frame("mlMinStopFrame", "mlMaxStopFrame", 50)
    paste = frame("mlMinCreamFrame", "mlMaxCreamFrame", 0)
    return mask, paste


def _declared_copper(designrules_el, layers_el, log) -> list[int]:
    """Which copper layers this board DECLARES it has.

    The answer is the design rules' own `layerSetup` — Eagle's stack-up
    formula, and the only place the board states its build: `(1*16)` for a
    two-layer board, `(1+2*15+16)` for a four-layer one. The numbers in
    order ARE the copper layers; `*` and `+` only say core or prepreg
    between them, which the thicknesses already carry.

    The `active` flag in `<layers>` is NOT this statement, and reading it
    as one was a real bug: Luminoso declares `(1*16)` — two layers — while
    still carrying `Route12` marked active from some earlier life of the
    board. A phantom third layer came out of that, and KiCad refused the
    result outright ("3 is not a valid layer count"). `active` is an
    editor's view setting; the stack-up is a fact about the board."""
    setup = _dru_params(designrules_el).get("layerSetup", "")
    declared = [int(n) for n in re.findall(r"\d+", setup) if 1 <= int(n) <= 16]
    if declared:
        return sorted(set(declared))
    active = {1, 16}
    if layers_el is not None:
        for layer_el in layers_el.findall("layer"):
            n = int(layer_el.get("number"))
            if 2 <= n <= 15 and layer_el.get("active", "yes") == "yes":
                active.add(n)
    log("design rules: no layerSetup formula — the stack was taken from the `active` flags in "
        "<layers>, which is an editor setting and can disagree with the real build")
    return sorted(active)


def check_declared_copper(board_el, declared: list[int]) -> None:
    """conversion-eagle.md #слои: copper drawn on a layer the stack never
    declared is not something to guess about.

    Two outcomes, and the difference is whether anything is actually
    THERE. A declared-but-unused layer is simply dropped — a leftover of
    an earlier build, and silence is right because nothing is lost. Copper
    on an UNDECLARED layer is the other case entirely: the board says one
    thing and contains another, and no reading of that is safe."""
    seen: dict[int, int] = {}
    for el in board_el.iter():
        raw = el.get("layer")
        if raw is None or not raw.lstrip("-").isdigit():
            continue
        n = abs(int(raw))
        if 1 <= n <= 16 and n not in declared:
            seen[n] = seen.get(n, 0) + 1
    if seen:
        rows = ", ".join(f"{n} ({count} объект(ов))" for n, count in sorted(seen.items()))
        raise ValueError(
            f"ЕСТЬ НЕПУСТЫЕ СЛОИ, НЕ ОПИСАННЫЕ В СТЕКЕ ПЛАТЫ, РАЗБЕРИСЬ ЧТО ТАМ ТВОРИТСЯ: "
            f"стек платы объявляет медь {declared}, а медь нарисована ещё и на слоях {rows}")


def derive_stack(layers_el, designrules_el, log) -> tuple[str, dict[int, int]]:
    """Returns (stack formula, {eagle layer number -> signed IR copper
    number}). conversion-eagle.md #слои: "Медь рождается из стека... номера
    вычисляются из формулы стека" — and the formula is `layerSetup`, see
    `_declared_copper`."""
    active = _declared_copper(designrules_el, layers_el, log)
    ir_numbers = copper_layer_numbers(len(active))
    eagle_to_ir = dict(zip(active, ir_numbers))

    params = _dru_params(designrules_el)
    mt_copper = params.get("mtCopper", "").split()
    mt_isolate = params.get("mtIsolate", "").split()
    if len(mt_copper) < 16 or len(mt_isolate) < 15:
        raise ValueError("design rules: mtCopper/mtIsolate incomplete — can't derive the physical stack")

    # mtIsolate is indexed by POSITION among this board's own active gaps
    # (0, 1, 2, ... for the 1st, 2nd, 3rd gap it actually has), not by raw
    # Eagle layer number — ground-truthed against step4 (a 2-layer board,
    # active {1,16}): summing mt_isolate[0:15] (every slot between raw
    # layer 1 and 16) gave a 4.02mm board where the real one is 1.57mm;
    # mt_isolate[0] alone gives the right 1.5mm core. The other 14 slots
    # are Eagle's own unused boilerplate for a stack-up this board never
    # has, not real material to sum in — board_export.py's own writer
    # already populates mtIsolate this same sequential way.
    parts = []
    for i, n in enumerate(active):
        parts.append(str(_dru_length(mt_copper[n - 1])))
        if i < len(active) - 1:
            gap = _dru_length(mt_isolate[i])
            parts.append(f"[{gap}]")
    return "".join(parts), eagle_to_ir


def make_layer_of(eagle_to_ir_copper: dict[int, int], log):
    """Board-space layer resolver: copper (1-16, only the active ones)
    comes from this board's own stack map; everything else reuses the
    fixed footprint/board table (geometry.py) — layer-model.md: "Слой в
    корпусе — тот же самый", so a board's own graphics read exactly the
    same table a placed footprint's would."""
    def layer_of(eagle_num: int):
        if eagle_num in eagle_to_ir_copper:
            signed = eagle_to_ir_copper[eagle_num]
            return Layer(abs(signed), 1 if signed >= 0 else -1)
        if 2 <= eagle_num <= 15:
            raise ValueError(f"content on Eagle copper layer {eagle_num}, which this board's "
                              f"active-layer stack ({sorted(eagle_to_ir_copper)}) doesn't include")
        return geo.footprint_layer(eagle_num, log)
    return layer_of


# ------------------------------------------------------------------ elements

_PAIRED_CANON = {1} | {canon for (canon, _bottom) in geo._FOOTPRINT_PAIRS.values()}

# A small marker symbol for a decorative library package with no pads at
# all — element.md #элемент-без-выводов: "просто квадратик" with the
# part's own name on it, not an attempt to reproduce the package's real
# artwork (Eagle already draws that from the footprint itself once the
# element has a real part behind it).
_LOGO_SQUARE = 2540
_LOGO_LINE_WIDTH = 152  # 6 mil (0.006"), Eagle's own common symbol-outline width
_LOGO_TEXT_HEIGHT = 1778
_LOGO_MARGIN = 5080
_LOGO_STEP = 10160
_LOGO_COLUMNS = 8


def _logo_symbol_graphics() -> list:
    half = _LOGO_SQUARE // 2
    corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    sides = [
        Line(x1=x1, y1=y1, x2=x2, y2=y2, width=_LOGO_LINE_WIDTH, layer=94)
        for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1])
    ]
    label = Text(x=0, y=half + 508, height=_LOGO_TEXT_HEIGHT, layer=95,
                 align="bottom-center", content=">NAME", ratio=8)
    return [*sides, label]


def _first_frame_bbox(schematic, symbols_by_key, components_by_key):
    """The canvas bbox of the first frame found in document order.
    schematic.py's own convert() already appends every sheet's <instance>s
    in sheet order while tiling them onto one flat canvas, so the first
    frame instance found here IS page one — no need to redo that sort."""
    parts_by_name = {p.name: p for p in schematic.parts}
    for inst in schematic.instances:
        part = parts_by_name.get(inst.part)
        if part is None:
            continue
        component = components_by_key.get((part.library, part.component))
        if component is None:
            continue
        gate = next((g for g in component.gates if g.name == inst.gate), None)
        if gate is None or component.library is None:
            continue
        symbol = symbols_by_key.get((component.library, gate.symbol))
        if symbol is None or not symbol.is_frame:
            continue
        frame_shape = geo.find_frame_shape(symbol)
        if frame_shape is None:
            continue
        return geo.bbox_from_shape(frame_shape, inst.mirror, inst.rot, inst.x, inst.y)
    return None


def synthesize_decorative_parts(board_el, footprints_by_key, parts_by_name, schematic, symbols, components, log) -> None:
    """element.md #элемент-без-выводов: a board-only `<element>` whose
    package carries no pads at all (a logo, a spacer) belongs on the
    schematic as a component without pins, placed by an ordinary `<part>`
    — never as raw board graphics, which element.md reserves for a
    one-off decal with no library backing at all ("разовую графику
    деталью заводить не надо"). A named, reusable library package like
    this one is exactly the "стойка, логотип" row of that page's own
    table: "да, деталью" — legal precisely because its footprint has no
    pad/smd at all (gate.md's own no-pin condition).

    Runs once per board, before convert_board's own element loop, and
    synthesizes one pin-less Symbol/Component per distinct decorative
    package plus one Part/ComponentInstance per placed element —
    mutating `schematic`/`symbols`/`components`/`parts_by_name` in place
    (the same post-hoc-mutation pattern already used for board-only
    attribute merging). Once done, convert_element's own ordinary lookup
    (name in parts_by_name) picks up every such `<element>` like any
    other — no separate board-side geometry code needed at all, so Eagle
    itself renders it, correctly, exactly like a real component."""
    symbols_by_key = {(s.library, s.name): s for s in symbols if s.name is not None}
    components_by_key = {(c.library, c.name): c for c in components}
    made: set[tuple[str, str]] = set()
    bbox = _first_frame_bbox(schematic, symbols_by_key, components_by_key)
    slot = 0

    for el_el in (board_el.find("elements") or []):
        name = el_el.get("name")
        if name in parts_by_name:
            continue
        library, package = el_el.get("library"), el_el.get("package")
        footprint = footprints_by_key.get((library, package))
        if footprint is None or footprint.pads:
            continue  # unresolved, or electrical — convert_element's own reject still applies

        comp_key = (library, package)
        if comp_key not in made:
            if comp_key in components_by_key or comp_key in symbols_by_key:
                raise ValueError(
                    f"board-only element {name!r}: would synthesize a pin-less pseudo-component "
                    f"named {package!r} in library {library!r} to stand in for it, but that name "
                    "is already taken there by something real"
                )
            symbol = Symbol(name=package, library=library, graphics=_logo_symbol_graphics())
            gate = Gate(symbol=package, name="G$1")
            device = Device(footprint=package, maps=[])
            component = Component(name=package, gates=[gate], prefix="U$", library=library, devices=[device])
            symbols.append(symbol)
            components.append(component)
            symbols_by_key[comp_key] = symbol
            components_by_key[comp_key] = component
            made.add(comp_key)
            log(f"board-only element {name!r} ({library}:{package}): no matching schematic part, but its "
                "package is pad-free — synthesized a pin-less pseudo-symbol/component so it can sit as "
                "a normal part (element.md #элемент-без-выводов), placed as a marker square on the "
                "schematic's first sheet")

        part = Part(name=name, component=package, library=library, device="")
        schematic.parts.append(part)
        parts_by_name[name] = part

        if bbox is not None:
            x0, _y0, _x1, y1 = bbox
            col, row = slot % _LOGO_COLUMNS, slot // _LOGO_COLUMNS
            px, py = x0 + _LOGO_MARGIN + col * _LOGO_STEP, y1 - _LOGO_MARGIN - row * _LOGO_STEP
        else:
            px = py = 0
            log(f"part {name!r}: no frame found on the schematic to place its marker inside — "
                "left at the canvas origin, move it manually")
        slot += 1
        schematic.instances.append(ComponentInstance(part=name, x=px, y=py, gate="G$1"))


def convert_element(el_el, footprints_by_key, parts_by_name, layer_of, auto: "_AutoSignals", log) -> Element | None:
    """Every board-only decorative element (a pad-free package with no
    schematic part behind it) has already been given a pin-less pseudo-
    part by `synthesize_decorative_parts`, run once before this loop
    starts — so by the time this runs, a name still missing from
    `parts_by_name` means something genuinely unresolvable: an unknown
    package, or a part-free package that DOES carry real pads (no
    signal/contactref system for those — that case still raises)."""
    name = el_el.get("name")
    x, y = geo.um(el_el.get("x")), geo.um(el_el.get("y"))
    mirror, rot = geo.angle(el_el.get("rot"))
    side = Side.BOTTOM if mirror else Side.TOP
    library, package = el_el.get("library"), el_el.get("package")
    footprint = footprints_by_key.get((library, package))

    if name not in parts_by_name:
        raise ValueError(
            f"board element {name!r} ({library}:{package}) has no matching schematic part — "
            "element.md: \"резолв имени обязан находить ровно одну <part>, без исключений\". "
            "A pad-free package should have been synthesized as a pin-less part automatically; "
            "this one wasn't, so either the package didn't resolve against any known footprint, "
            "or its footprint carries real pads (an electrical element needs a genuine part)."
        )

    texts = []
    if el_el.get("smashed") == "yes":
        declared: set[str] = set()
        if footprint is None:
            log(f"element {name!r}: package {library}:{package} not resolved against the "
                f"schematic's footprint pool (likely a part inside a module channel) — "
                f"placeholder-position overrides skipped")
        else:
            declared = {
                g.content[1:].split("@")[0].upper()
                for g in footprint.graphics if isinstance(g, Text) and g.content.startswith(">")
            }
        part = parts_by_name.get(name)
        for a in el_el.findall("attribute"):
            key = a.get("name", "")
            key_upper = key.upper()
            if key_upper in declared:
                amirror, arot = geo.angle(a.get("rot"))
                align = _ALIGN_MAP.get(a.get("align", "bottom-left"), "bottom-left")
                a_signed, _a_anti = geo._split(layer_of(int(a.get("layer", "25"))))
                texts.append(Text(
                    x=geo.um(a.get("x")), y=geo.um(a.get("y")),
                    height=geo.um(a.get("size", "1.778")), layer=a_signed,
                    align=align, content=f">{key_upper}", rot=arot, mirror=amirror,
                    ratio=int(a.get("ratio", "8")),
                ))
                continue
            # conversion-eagle.md #атрибуты: a real, valued attribute
            # display (MANF, LCSC#, ...), not a placeholder override.
            # Eagle duplicates part attrs onto the board's own element;
            # the schematic's own copy wins on a key collision, and a
            # board-only key is merged in as-is.
            value = a.get("value")
            if value is None or part is None:
                continue
            new_attr = geo.safe_attr(key, value, log)
            # Compare against the SANITIZED name, not the raw one — a
            # module-channel part is one shared object seen through every
            # channel's own element (IC101, IC102, ...), each carrying
            # its own copy of the same raw key; comparing raw-vs-already-
            # sanitized let every channel re-add its own copy.
            if not any(pa.name.lower() == new_attr.name.lower() for pa in part.attrs):
                part.attrs.append(new_attr)

    return Element(name=name, x=x, y=y, rot=rot, side=side, texts=texts)


# ----------------------------------------------------------------------- via



def convert_via(via_el, log, restring=None) -> Via:
    x, y = geo.um(via_el.get("x")), geo.um(via_el.get("y"))
    drill = geo.um(via_el.get("drill"))
    extent = via_el.get("extent")
    if extent != "1-16":
        # via.md: only through vias are expressible — a real through via
        # always spans the whole 16-position nominal template regardless
        # of how many of those positions this board actually uses
        # (ground-truthed: every via in all 5 projects reads "1-16", even
        # on the 2-layer boards); anything else is genuinely blind/buried.
        raise ValueError(f"via at ({x},{y}): extent {extent!r} is not a through via "
                          "(via.md: blind/buried vias are not expressible in IR)")
    rules = restring if restring is not None else geo.Restring()
    diameter_s = via_el.get("diameter", "0")
    if float(diameter_s) == 0.0:
        diameter = rules.diameter("via_outer", drill)
    else:
        diameter = geo.um(diameter_s)
    # via.md #кольцо-на-внутренних-слоях: the inner ring is its own number
    # and is ALWAYS computed — an explicit `diameter` names the outer one
    # only. That 25 µm per side is what makes an Eagle board pass its own
    # DRC where a KiCad copy of it does not.
    inner = rules.diameter("via_inner", drill)
    inner_dia = inner if inner != diameter else None
    shape = via_el.get("shape", "round")
    if shape != "round":
        log(f"via at ({x},{y}): shape {shape!r} has no IR equivalent -> round")
    return Via(x=x, y=y, drill=drill, diameter=diameter, inner_dia=inner_dia)


# ------------------------------------------------------------------- plating

def _wire_to_plating_segment(child, log):
    x1, y1, x2, y2 = geo.um(child.get("x1")), geo.um(child.get("y1")), geo.um(child.get("x2")), geo.um(child.get("y2"))
    curve = child.get("curve")
    if curve is not None and float(curve) != 0.0:
        from .geometry import _signed_mdeg
        return PlatingArc(x1, y1, x2, y2, _signed_mdeg(curve))
    return PlatingLine(x1, y1, x2, y2)


def _polygon_to_plating_path(el, log) -> list:
    """A closed contour on layer 46 (rare, unverified against real ground
    truth — no test project uses plating at all) decomposes into the same
    per-edge Line/Arc chain a regular polygon would, just typed as
    PlatingLine/PlatingArc instead."""
    from .geometry import _signed_mdeg
    pts = [(geo.um(v.get("x")), geo.um(v.get("y")), v.get("curve")) for v in el.findall("vertex")]
    path = []
    n = len(pts)
    for i in range(n):
        x1, y1, curve = pts[i]
        x2, y2, _ = pts[(i + 1) % n]
        if curve is not None and float(curve) != 0.0:
            path.append(PlatingArc(x1, y1, x2, y2, _signed_mdeg(curve)))
        else:
            path.append(PlatingLine(x1, y1, x2, y2))
    return path


def _make_plating(children, log) -> Plating:
    """conversion-eagle.md #слои: Eagle has no native plating object — a
    metallized cut is just ordinary geometry drawn on `46 Milling`. `land`/
    `inner_land` can't be recovered (Eagle doesn't store a strip width at
    all), so both come back zero, logged once per occurrence."""
    log("layer 46 (Milling) geometry treated as metallized — land/inner_land "
        "defaulted to 0 (Eagle stores no strip width); verify this really is plating")
    path = []
    for child in children:
        if child.tag == "wire":
            path.append(_wire_to_plating_segment(child, log))
        elif child.tag == "polygon":
            path.extend(_polygon_to_plating_path(child, log))
    return Plating(path=path, land=0, inner_land=0)


# ------------------------------------------------------------- plain/copper

class _AutoSignals:
    """conversion-eagle.md #безымянная-медь: copper drawn on its own (no
    <contactref>, sometimes not even inside a <signal> at all when it sits
    in <plain>) gets a synthesized name — Eagle's own convention, `N$n`."""

    def __init__(self):
        self.n = 0
        self.signals: dict[str, Signal] = {}
        self.plain_graphics: list = []

    def new(self) -> Signal:
        self.n += 1
        name = f"N${self.n}"
        sig = Signal(name=name)
        self.signals[name] = sig
        return sig


def _convert_plain(plain_el, layer_of, auto: _AutoSignals, log) -> tuple[list, list[Hole]]:
    graphics, holes = [], []
    if plain_el is None:
        return graphics, holes

    # Layer 46 (Milling) wires/polygons batch into ONE Plating path, not
    # one per element — a plated slot is normally drawn as several
    # connected wire segments, and each deserves one signal, not four.
    milling = [c for c in plain_el if c.tag in ("wire", "polygon") and int(c.get("layer")) == 46]
    if milling:
        sig = auto.new()
        sig.copper.append(_make_plating(milling, log))

    for child in plain_el:
        if child.tag == "hole":
            holes.append(Hole(geo.um(child.get("x")), geo.um(child.get("y")), geo.um(child.get("drill"))))
            continue
        if child in milling:
            continue
        if child.tag not in ("wire", "text", "circle", "rectangle", "polygon"):
            log(f"board: decorative <{child.tag}> on the canvas has no IR equivalent — dropped")
            continue

        if child.tag == "wire":
            g = geo.convert_wire(child, layer_of)
            if g is None:            # короче микрона — см. geo.convert_wire
                continue
        elif child.tag == "text":
            g = geo.convert_text(child, layer_of)
        elif child.tag == "circle":
            g = geo.convert_circle(child, layer_of)
        elif child.tag == "rectangle":
            g = geo.convert_rectangle(child, layer_of)
        else:  # polygon
            g = geo.convert_polygon(child, layer_of, log)
            if g is None:
                continue

        if isinstance(g, (Line, Arc, Polygon)) and not g.anti and abs(g.layer) < 100:
            # signal.md #безымянная-медь: copper drawn free-floating, not
            # inside any <signal> — a legal Eagle idiom (e.g. a hand-routed
            # trace before naming its net), given its own auto-name here.
            sig = auto.new()
            sig.copper.append(g)
            continue
        graphics.append(g)
    return graphics, holes


# -------------------------------------------------------------------- signal

_LAYER_UNROUTED = 19
"""Eagle's "Unrouted" layer — where it draws the airwires of connections the
board does not yet route. Derived from the netlist, never authored."""


def convert_signal(signal_el, layer_of, log, restring=None) -> tuple[Signal, list]:
    """Returns (signal, stray_anti) — `stray_anti` holds any cutout/anti
    geometry Eagle drew inside this <signal> (a cutout pour), which
    layer-model.md says can never actually live in a <signal>: it belongs
    on the board directly, since it cuts every polygon on its layer, not
    just this one signal's own."""
    name = signal_el.get("name")
    copper, contactrefs, stray_anti = [], [], []

    # Layer 46 (Milling) wires/polygons batch into ONE Plating path — see
    # _convert_plain's identical reasoning for the bare-board case.
    milling = [c for c in signal_el if c.tag in ("wire", "polygon") and int(c.get("layer")) == 46]
    if milling:
        copper.append(_make_plating(milling, log))

    for child in signal_el:
        if child in milling:
            continue
        if child.tag == "wire" and int(child.get("layer")) == _LAYER_UNROUTED:
            # Eagle's own ratsnest: an unrouted connection drawn as a wire on
            # layer 19. It is not copper (signal.md would reject it as such,
            # rightly) and not a fact either — Eagle recomputes it from the
            # netlist on every load, exactly like the <junction> the schematic
            # side already drops. Keeping it would mean storing a derivative.
            continue
        if child.tag == "contactref":
            contactrefs.append(ContactRef(element=child.get("element"), pad=child.get("pad")))
        elif child.tag == "via":
            copper.append(convert_via(child, log, restring))
        elif child.tag in ("wire", "polygon"):
            if child.tag == "wire":
                g = geo.convert_wire(child, layer_of)
                if g is None:        # короче микрона — см. geo.convert_wire
                    continue
            else:
                g = geo.convert_polygon(child, layer_of, log)
                if g is None:
                    continue
                if isinstance(g, Polygon) and not g.anti:
                    rank = int(child.get("rank", "1")) or 1
                    thermals = 1 if child.get("thermals", "yes") == "yes" else 0
                    clearance = geo.um(child.get("isolate", "0"))
                    if child.get("pour", "solid") == "hatch":
                        log(f"signal {name!r}: hatched polygon simplified to solid fill "
                            "(conversion-eagle.md: hatching isn't kept)")
                    g = Polygon(layer=g.layer, width=g.width, vertices=g.vertices, fill=100,
                                rank=rank, thermals=thermals, clearance=clearance, anti=g.anti)
            if g.anti:
                log(f"signal {name!r}: cutout/anti geometry pulled out to the board "
                    "(layer-model.md: anti-objects don't live inside a signal)")
                stray_anti.append(g)
                continue
            copper.append(g)
        else:
            log(f"signal {name!r}: unexpected <{child.tag}> — dropped")
    return Signal(name=name, contactrefs=contactrefs, copper=copper), stray_anti


# --------------------------------------------------------------- top level

def convert_board(board_el, layers_el, board_name: str, footprints_by_key, parts_by_name, log) -> Layout:
    designrules_el = board_el.find("designrules")
    stack, eagle_to_ir_copper = derive_stack(layers_el, designrules_el, log)
    # Before anything is read off the board: does what it CONTAINS agree
    # with what it DECLARES? An undeclared layer with copper on it stops
    # the import here, where the whole board is still in front of us and
    # every offending layer can be named at once.
    check_declared_copper(board_el, sorted(eagle_to_ir_copper))
    rules = convert_rules(designrules_el, log)
    mask_expansion, paste_expansion = convert_expansions(designrules_el, log)
    layer_of = make_layer_of(eagle_to_ir_copper, log)

    auto = _AutoSignals()
    plain_graphics, holes = _convert_plain(board_el.find("plain"), layer_of, auto, log)

    elements = []
    for el_el in (board_el.find("elements") or []):
        element = convert_element(el_el, footprints_by_key, parts_by_name, layer_of, auto, log)
        if element is not None:
            elements.append(element)

    signals = list(auto.signals.values())
    anti_graphics = []
    for signal_el in (board_el.find("signals") or []):
        sig, stray_anti = convert_signal(signal_el, layer_of, log, geo.Restring(_dru_params(designrules_el)))
        signals.append(sig)
        anti_graphics.extend(stray_anti)

    attrs_el = board_el.find("attributes")
    global_attrs = [geo.safe_attr(a.get("name"), a.get("value", ""), log)
                    for a in (attrs_el if attrs_el is not None else [])]

    return Layout(
        name=board_name, stack=stack, mask_expansion=mask_expansion, paste_expansion=paste_expansion,
        rules=rules, elements=elements, signals=signals,
        graphics=plain_graphics + anti_graphics + auto.plain_graphics,
        holes=holes, attrs=global_attrs,
    )
