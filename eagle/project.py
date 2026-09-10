"""Eagle project entry point — the `eagle.project.import_project` main.py
already expects. conversion-eagle.md #что-приготовить: a board is optional
(a schematic-only Eagle project is legal, project.md allows zero layouts),
but if a sibling `.brd` exists next to the `.sch` — same stem, same
directory, Eagle's own convention — it's read too.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from ir.module_instance import expand_designator
from ir.project import Project

from . import board as board_conv
from . import geometry as geo
from . import schematic as sch_conv
from import_log import log


def _resolve_parts_by_name(schematic, modules) -> dict[str, object]:
    """Every name a board <element> is allowed to resolve to, mapped to
    the actual `Part` it names: real top-level parts, plus every
    module-channel part under its expanded designator
    (module-instance.md) — Eagle's board is flat, no module concept at
    all, so a channeled part shows up there under the name a flattening
    consumer would give it. The `Part` itself is needed, not just its
    name, so a board-only `<element><attribute>` (conversion-eagle.md
    #атрибуты: Eagle duplicates part attrs onto the board) has something
    real to merge into."""
    by_name = {p.name: p for p in schematic.parts}
    modules_by_name = {m.name: m for m in modules}
    for mi in schematic.modinsts:
        module = modules_by_name.get(mi.module)
        if module is None:
            continue
        for part in module.schematic.parts:
            by_name[expand_designator(part.name, mi.name, mi.offset)] = part
    return by_name


def import_project(path: Path) -> Project:
    root = ET.parse(path).getroot()
    sch_el = root.find("drawing/schematic")
    if sch_el is None:
        raise ValueError(f"{path}: not an Eagle schematic (no <drawing><schematic>)")

    # NoABS.Eagle3d (conversion-eagle.md #3d-модели, model3d.md): models
    # live in a folder sibling to the project file, named after its own
    # stem — `step4.sch` -> `step4/`, ground-truthed against a real
    # project the user populated this way.
    models_dir = Path(path).parent / Path(path).stem

    # **The board's design rules are read BEFORE the libraries.** Eagle's
    # annular ring is a rule, not a stored number: a pad or via drawn with
    # AUTO diameter has none of its own, and even one drawn with an
    # explicit diameter still takes its INNER ring from the rules
    # (via.md #кольцо-на-внутренних-слоях). Footprints are converted from
    # the schematic's libraries, so the rules have to arrive first or every
    # such pad gets Eagle's bare defaults instead of this board's.
    brd_path = Path(path).with_suffix(".brd")
    brd_root = ET.parse(brd_path).getroot() if brd_path.is_file() else None
    restring = geo.Restring()
    if brd_root is not None:
        dr = brd_root.find("drawing/board/designrules")
        if dr is not None:
            restring = geo.Restring({p.get("name"): p.get("value") for p in dr.findall("param")})

    schematic, symbols, footprints, components, classes, modules = sch_conv.convert(
        sch_el, log, models_dir, restring)

    layouts = []
    if brd_root is not None:
        board_el = brd_root.find("drawing/board")
        if board_el is None:
            raise ValueError(f"{brd_path}: not an Eagle board (no <drawing><board>)")
        layers_el = brd_root.find("drawing/layers")

        footprints_by_key = {(f.library, f.name): f for f in footprints}
        # A board-only decorative element (a logo added straight to the
        # board, no schematic part behind it — element.md) names a package
        # that never went through any device, so it never entered the
        # schematic's own footprint pool at all; it only exists in the
        # board's own embedded <libraries>, same merge-keep-first policy
        # as schematic.py's own merge_libraries.
        board_libs_el = board_el.find("libraries")
        if board_libs_el is not None:
            _board_symbols, board_footprints, _board_components, _board_uservalue, _board_renames = \
                sch_conv.merge_libraries(board_libs_el, log, models_dir, restring)
            for f in board_footprints:
                key = (f.library, f.name)
                if key not in footprints_by_key:
                    footprints_by_key[key] = f
                    footprints.append(f)

        parts_by_name = _resolve_parts_by_name(schematic, modules)
        board_conv.synthesize_decorative_parts(
            board_el, footprints_by_key, parts_by_name, schematic, symbols, components, log)
        layouts.append(board_conv.convert_board(
            board_el, layers_el, brd_path.stem, footprints_by_key, parts_by_name, log))

    return Project(
        name=Path(path).stem,
        version=(1, 0),  # version.md: current format version, written explicitly
        schematic=schematic,
        symbols=symbols,
        footprints=footprints,
        components=components,
        classes=classes,
        modules=modules,
        layouts=layouts,
    )
