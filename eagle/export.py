"""IR `Project` -> a self-contained Eagle project folder: `<name>.sch`,
`<name>.brd` (if there's a board), and one `<library>.lbr` per library
used — Eagle's own convention (a project is its schematic/board pair plus
the library files they draw on, not one file with everything baked in).
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from ir.project import Project
from ir.stack import parse_stack

from . import board_export, schematic_export
from . import library_export as lib_export
from import_log import log
from ir import model3d

_TEMPLATE_DIR = Path(__file__).parent
_EAGLE_VERSION = "9.6.2"  # matches the ground-truth layers table below


def _settings_and_grid() -> tuple[ET.Element, ET.Element]:
    settings = ET.Element("settings")
    ET.SubElement(settings, "setting", alwaysvectorfont="no")
    ET.SubElement(settings, "setting", verticaltext="up")
    grid = ET.Element("grid", distance="0.1", unitdist="inch", unit="inch", style="lines",
                       multiple="1", display="no", altdistance="0.01", altunitdist="inch", altunit="inch")
    return settings, grid


def _layers_table() -> ET.Element:
    """The canonical Eagle layer palette — cosmetic data the IR doesn't
    govern at all (layer-model.md: "поставляется вместе с редактором и
    конвертером"), taken verbatim from a real Eagle 9.6 SCHEMATIC file.
    `active`/`visible` there reflect a schematic's own needs (91-99
    active, essentially every board-space layer inactive) — fine for a
    `.sch`'s own cosmetic table, wrong for a board's (see
    `_board_layers_table`, which starts from a different, board-sourced
    template instead of patching this one)."""
    return ET.parse(_TEMPLATE_DIR / "eagle_layers_template.xml").getroot()


def _board_layers_table(copper_count: int) -> ET.Element:
    """The board-space counterpart of `_layers_table` — taken verbatim
    from a real Eagle BOARD file (`.brd`, not `.sch`: the two disagree on
    which layers are `active`, since a schematic's own copy of this table
    only cares about 91-99 and leaves every board layer inactive, ground
    truth confirmed against tolmach.brd vs. tolmach.sch). Reusing the
    schematic's template here left every real board layer — 1-61 outside
    the 1/16 this function already patched, including plain 20 Dimension,
    21 tPlace, 25 tNames... — inactive, so this board's own graphics had
    nowhere to render even though they were all correctly in the file.

    Only the copper 1-16 entries' `active`/`visible` get patched here, to
    match this board's own stack (board_export.py's contiguous-from-the-
    top convention) — everything else is already right as extracted."""
    root = ET.parse(_TEMPLATE_DIR / "eagle_board_layers_template.xml").getroot()
    active = {1, 16} | set(range(2, copper_count))
    for layer_el in root.findall("layer"):
        n = int(layer_el.get("number"))
        if 1 <= n <= 16:
            layer_el.set("active", "yes" if n in active else "no")
            layer_el.set("visible", "yes" if n == 1 else "no")
    return root


def export_project(project: Project) -> ET.Element:
    root = ET.Element("eagle", version=_EAGLE_VERSION)
    drawing = ET.SubElement(root, "drawing")
    settings, grid = _settings_and_grid()
    drawing.append(settings)
    drawing.append(grid)
    drawing.append(_layers_table())
    drawing.append(schematic_export.write_schematic(project, log))
    return root


def export_board(layout, project: Project) -> ET.Element:
    coppers, _dielectrics = parse_stack(layout.stack)
    root = ET.Element("eagle", version=_EAGLE_VERSION)
    drawing = ET.SubElement(root, "drawing")
    settings, grid = _settings_and_grid()
    drawing.append(settings)
    drawing.append(grid)
    drawing.append(_board_layers_table(len(coppers)))
    drawing.append(board_export.write_board(layout, project, log))
    return root


def export_library(name: str, symbols, footprints, components) -> ET.Element:
    """A standalone `.lbr` — same `<library>` content schematic/board
    export embed inline, just as the drawing's own root content instead
    of nested under a `<libraries>` wrapper (eagle.dtd: `drawing`'s single
    child is one of `library | schematic | board`)."""
    root = ET.Element("eagle", version=_EAGLE_VERSION)
    drawing = ET.SubElement(root, "drawing")
    settings, grid = _settings_and_grid()
    drawing.append(settings)
    drawing.append(grid)
    drawing.append(_layers_table())
    lib_export.write_library(drawing, name, symbols, footprints, components, log)
    return root


def _copy_3d_models(footprints, source_models_dir: Path | None, dest_models_dir: Path, log) -> None:
    """NoABS.Eagle3d, export half (conversion-eagle.md #3d-модели):
    `write_footprint` already wrote the placement numbers back into each
    footprint's own `<description>` unconditionally — copying the actual
    STEP source file is the separate, best-effort other half, since only
    the numbers are IR's to keep (model3d.md: the file name isn't stored,
    it's derived). No `source_models_dir` at all means nothing to copy
    from — logged once, not per footprint."""
    footprints_with_models = [f for f in footprints if f.models]
    if not footprints_with_models:
        return
    if source_models_dir is None:
        log("3D models: no source model folder given — placement metadata was written back, "
            "but no STEP files were copied")
        return
    copied = 0
    for f in footprints_with_models:
        for m in f.models:
            src = model3d.find_file(source_models_dir, f.name, m.key)
            if src is None:
                log(f"footprint {f.library}:{f.name}: no {model3d.base_name(f.name, m.key)}.step/.stp "
                    f"in {source_models_dir} — placement metadata was written, but the model wasn't copied")
                continue
            dest_models_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest_models_dir / src.name)
            copied += 1
    if copied:
        log(f"3D models: copied {copied} STEP file(s) into {dest_models_dir}")


def _write_xml(root: ET.Element, path: Path) -> None:
    tree = ET.ElementTree(root)
    ET.indent(tree, space=" ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def _group_by_library(project: Project) -> dict[str, tuple[list, list, list]]:
    by_lib: dict[str, tuple[list, list, list]] = {}
    for s in project.symbols:
        by_lib.setdefault(s.library, ([], [], []))[0].append(s)
    for f in project.footprints:
        by_lib.setdefault(f.library, ([], [], []))[1].append(f)
    for c in project.components:
        by_lib.setdefault(c.library, ([], [], []))[2].append(c)
    return by_lib


def write_project(project: Project, path: Path, source_models_dir: Path | None = None) -> Path:
    """Writes a self-contained project folder named after `path`'s stem —
    `<stem>.sch`, `<stem>.brd` (if there's a board), one `<library>.lbr`
    per library used — and returns that folder. Mirrors Eagle's own habit
    of shipping a project as its schematic/board pair plus the library
    files they draw on, not one file with everything baked in.

    `source_models_dir` is NoABS.Eagle3d's own concern (conversion-eagle.md
    #3d-модели): the folder the original STEP files live in, so any
    footprint carrying a `Model3D` gets its file copied alongside the
    project's own `<stem>/` sibling folder — the placement numbers
    written into `<description>` are unconditional either way, since only
    they are IR's to keep at all."""
    path = Path(path)
    out_dir = path.parent / path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_xml(export_project(project), out_dir / f"{path.stem}.sch")

    if len(project.layouts) > 1:
        raise ValueError(f"eagle export: {len(project.layouts)} layouts on one project — Eagle "
                          "pairs exactly one board with one schematic; export each separately")
    if project.layouts:
        _write_xml(export_board(project.layouts[0], project), out_dir / f"{path.stem}.brd")

    _copy_3d_models(project.footprints, source_models_dir, out_dir / path.stem, log)

    for lib_name, (syms, fps, comps) in _group_by_library(project).items():
        _write_xml(export_library(lib_name, syms, fps, comps), out_dir / f"{lib_name}.lbr")

    return out_dir
