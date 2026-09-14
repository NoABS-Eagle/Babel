"""KiCad project entry point — the `kicad.project.import_project` main.py
already expects. Input is the path to a `.kicad_pro`
(conversion-kicad.md #что-приготовить).

This module does the reading and the refusing, not the converting: it
finds the files a project is made of, checks the preconditions the spec
states, and hands the rest a source that is known to be whole. Everything
it rejects, it rejects here — before a single object has been converted,
so the message names a file and not a half-built tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from import_log import log
from ir.attr import Attr
from ir.project import Project

from . import board as board_conv
from . import footprint as footprint_conv
from . import library as library_conv
from . import schematic as schematic_conv
from . import sexpr

# The format version that says "KiCad 10". Read off real files rather than
# reasoned about: KiCad 7 wrote 20221018 (pcb) / 20230121 (sch), 8 wrote
# 20231120, 9 wrote 20250114, and every KiCad 10 file in testData —
# 20260101, 20260206, 20260306 — is a 2026 stamp. The boundary is the year.
_KICAD_10 = 20260000


@dataclass
class Source:
    """A KiCad project read off disk and found whole — the raw trees, not
    yet anything of IR's."""
    name: str
    pro_path: Path
    settings: dict
    board_path: Path
    board: sexpr.Node
    root_pages: list[Path]
    """Top-level pages, in the order `.kicad_pro` lists them. Each becomes
    a frame on the one canvas (conversion-kicad.md #схема)."""
    sheets: dict[Path, sexpr.Node] = field(default_factory=dict)
    """Every schematic file of the project, root pages included."""
    placements: dict[Path, int] = field(default_factory=dict)
    """How many times each file is placed as a sub-sheet. One placement
    flattens into a page, several make a module — so this is the fact the
    hierarchy hangs on (conversion-kicad.md #схема)."""
    footprint_lib: dict[str, Path] = field(default_factory=dict)
    """Footprint name -> its `.kicad_mod` file."""
    footprints: dict = field(default_factory=dict)
    """Footprint name -> the converted library footprint. This is the
    geometry the project gets: it belongs to the library, and every
    placement on the board has been checked against it."""


def _sheet_file(sheet: sexpr.Node) -> str | None:
    """The file a `(sheet …)` node points at, out of its properties."""
    for prop in sexpr.kids(sheet, "property"):
        atoms = sexpr.atoms(prop)
        if len(atoms) >= 2 and atoms[0] == "Sheetfile":
            return str(atoms[1])
    return None


def _read_sheets(root_pages: list[Path], sheets: dict, placements: dict) -> None:
    """Walk the sheet tree breadth-first from the top-level pages, reading
    each file once and counting how often it is placed."""
    queue = list(root_pages)
    while queue:
        path = queue.pop(0)
        if path in sheets:
            continue
        if not path.is_file():
            raise SystemExit(f"{path.name}: sheet file named by the project is missing")
        sheets[path] = sexpr.load(path)
        for sheet in sexpr.kids(sheets[path], "sheet"):
            name = _sheet_file(sheet)
            if name is None:
                continue
            child = (path.parent / name).resolve()
            placements[child] = placements.get(child, 0) + 1
            queue.append(child)


def _check_versions(sheets: dict[Path, sexpr.Node], board_path: Path,
                    board: sexpr.Node) -> None:
    """conversion-kicad.md #плата: a format older than KiCad 10 is refused
    with a demand to resave. Every file is named, not just the first —
    a project edited by several KiCad versions carries a mix of them, and
    the user needs the whole list to know one resave will not do."""
    old = []
    for path, tree in list(sheets.items()) + [(board_path, board)]:
        version = sexpr.kid(tree, "version")
        stamp = sexpr.atoms(version)[0] if version else None
        if not isinstance(stamp, int):
            raise SystemExit(f"{path.name}: no format version — not a KiCad file")
        if stamp < _KICAD_10:
            old.append(f"  {path.name}: {stamp}")
    if old:
        raise SystemExit(
            "This project predates KiCad 10:\n" + "\n".join(sorted(old)) +
            "\nOpen it in KiCad 10 and save it, then convert.")


def _read_footprint_lib(project_dir: Path) -> dict[str, Path]:
    """Every `.kicad_mod` anywhere under the project folder, by footprint
    name. Resolved by bare name, not by library nickname: a footprint's
    nickname on the board routinely names a library the project no longer
    registers — real case, a catalogue converted out of Altium, where the
    board says `Resistors SMD:…` and the one registered `.pretty` is
    called something else entirely."""
    lib: dict[str, Path] = {}
    for path in sorted(project_dir.rglob("*.kicad_mod")):
        # `.history` is KiCad's local-history plugin: superseded copies of
        # files still in the project. Reading one would silently hand back
        # an older footprint than the board actually uses.
        if any(part == ".history" for part in path.parts):
            continue
        lib.setdefault(path.stem, path)
    return lib


def _check_footprint_lib(board: sexpr.Node, lib: dict[str, Path]) -> None:
    """conversion-kicad.md #что-отвергается: without the library there is
    nowhere to read the default placeholder layout from — on the board
    every instance has already overridden it."""
    missing = set()
    for fp in sexpr.kids(board, "footprint"):
        atoms = sexpr.atoms(fp)
        if not atoms:
            continue
        lib_id = str(atoms[0])
        if lib_id.rsplit(":", 1)[-1] not in lib:
            missing.add(lib_id)
    if missing:
        shown = "\n".join(f"  {name}" for name in sorted(missing)[:20])
        more = f"\n  … and {len(missing) - 20} more" if len(missing) > 20 else ""
        raise SystemExit(
            f"{len(missing)} of the footprints this board uses are not in the "
            f"project folder:\n" + shown + more +
            "\nIn KiCad: FILE -> EXPORT -> FOOTPRINTS, into the project folder. "
            "Their default placeholder layout is only in the library — the "
            "board carries per-instance overrides and nothing else.")


def read_source(path: Path) -> Source:
    path = Path(path).resolve()
    settings = json.loads(path.read_text(encoding="utf-8"))
    project_dir = path.parent

    # conversion-kicad.md #в-kicad: since version 10 the top-level pages
    # are listed here, and there is no synthesized root sheet. An older
    # project has no such field; fall back to KiCad's own file-naming
    # convention so that the version check below gets something to read
    # and can name the actual file.
    tops = (settings.get("schematic") or {}).get("top_level_sheets") or []
    root_pages = [(project_dir / t["filename"]).resolve()
                  for t in tops if t.get("filename")]
    if not root_pages:
        root_pages = [path.with_suffix(".kicad_sch")]

    board_path = path.with_suffix(".kicad_pcb")
    if not board_path.is_file():
        raise SystemExit(
            f"{board_path.name} not found. A KiCad schematic is converted "
            "together with its board — conversion-kicad.md #что-приготовить.")
    board = sexpr.load(board_path)

    sheets: dict[Path, sexpr.Node] = {}
    placements: dict[Path, int] = {}
    _read_sheets(root_pages, sheets, placements)

    _check_versions(sheets, board_path, board)

    lib = _read_footprint_lib(project_dir)
    _check_footprint_lib(board, lib)

    # conversion-kicad.md #правка-на-размещении-отвергает-проект. The
    # reference is the library file, never a neighbouring placement: a
    # whole board's instances can agree with each other and still all be
    # edited, if one hand did it once and copied the part around.
    footprints = footprint_conv.load_library(lib, log)
    complaints = footprint_conv.check_board(board, footprints, log)
    if complaints:
        shown = "\n".join(complaints[:15])
        more = f"\n  … and {len(complaints) - 15} more" if len(complaints) > 15 else ""
        raise SystemExit(
            f"{len(complaints)} placed footprint(s) do not match the library they "
            f"name — their copper or silkscreen was edited on the board:\n"
            + shown + more +
            "\nIn KiCad: 'Update Footprints from Library', or put the edit into "
            "the library where it belongs. Geometry belongs to the footprint, "
            "and a placement carries only where it sits.")

    return Source(
        name=path.stem,
        pro_path=path,
        settings=settings,
        board_path=board_path,
        board=board,
        root_pages=root_pages,
        sheets=sheets,
        placements=placements,
        footprint_lib=lib,
        footprints=footprints,
    )


# Blank canvas between two pages. They only need to not touch: the sheet
# boundary is not geometry, and nothing of one page may reach another.
_PAGE_GAP = 10000


def build_schematic(source: Source, components: list, symbols: list, log):
    """Every sheet of the project, laid out on the one canvas
    (conversion-kicad.md #схема). A sheet placed once flattens into a page;
    the root pages come first, in the order `.kicad_pro` lists them."""
    from ir.schematic import Schematic

    by_lib_id = {f"{c.library}:{c.name}" if c.library else c.name: c
                 for c in components}
    by_key = {(s.library, s.name): s for s in symbols}

    order = list(source.root_pages) + [p for p in source.sheets
                                       if p not in source.root_pages]
    pages: dict = {}
    x0 = 0
    for path in order:
        size = schematic_conv.page_size(source.sheets[path], log, path.name)
        pages[path] = schematic_conv.Page((x0, size[1]), size)
        x0 += size[0] + _PAGE_GAP

    parts, instances, graphics, notes = [], [], [], []
    seen_parts: dict = {}
    conn = schematic_conv.Connectivity()
    labels, sheet_pins, ports, stamps = [], [], {}, []

    for path, page in pages.items():
        tree = source.sheets[path]
        depth = 0 if path in source.root_pages else 1
        for node in sexpr.kids(tree, "symbol"):
            got = schematic_conv.convert_placement(node, page, by_lib_id, log, path.name)
            if got is not None:
                # One part, many placements: a multi-section part puts one
                # section on each page, and KiCad writes a full symbol for
                # each. part.md has a single <part> per designator.
                if got[0].name not in seen_parts:
                    seen_parts[got[0].name] = got[0]
                    parts.append(got[0])
                instances.append(got[1])
        schematic_conv.read_wires(tree, page, conn)
        labels += schematic_conv.read_labels(tree, page, conn, depth)
        for point, name, filename, _node in schematic_conv.read_sheet_pins(tree, page):
            conn.touch(point)
            sheet_pins.append((point, name, (path.parent / filename).resolve()))
        ports[path] = schematic_conv.read_hierarchical_ports(tree, page)
        for point in ports[path].values():
            conn.touch(point)
        page_graphics, page_notes = schematic_conv.convert_graphics(tree, page, log, path.name)
        graphics += page_graphics + schematic_conv.sheet_graphics(tree, page, log, path.name)
        notes += page_notes
        stamps.append(schematic_conv.stamp_fields(tree))

    bridges = [(point, ports.get(child, {}).get(name), name)
               for point, name, child in sheet_pins
               if ports.get(child, {}).get(name) is not None]

    pins = schematic_conv.read_pins(instances, parts, by_lib_id, by_key,
                                    None, conn, log, source.name)
    nets = schematic_conv.build_nets(conn, labels, pins, parts, log,
                                      source.name, bridges)

    # frame.md: every page gets a frame, and the stamp splits by how many
    # pages share each field.
    shared, per_page = schematic_conv.split_stamp(stamps)
    frame_symbols: dict[str, object] = {}
    for number, (path, page) in enumerate(pages.items(), start=1):
        symbol = schematic_conv.frame_symbol((page.width, page.height), source.name)
        frame_symbols.setdefault(symbol.name, symbol)
        component, part, instance = schematic_conv.frame_part(
            symbol.name, source.name, f"FRAME{number}", page, number,
            _sheet_name(source, path), per_page[number - 1])
        if not any(c.name == component.name for c in components):
            components.append(component)
        parts.append(part)
        instances.append(instance)
    symbols += frame_symbols.values()

    return Schematic(parts=parts, instances=instances, nets=nets,
                     graphics=graphics, notes=notes,
                     attrs=[Attr(k, v) for k, v in sorted(shared.items())])


def _net_by_pad(schematic, components: list) -> dict:
    """(designator, pad) -> the net the SCHEMATIC wires it to.

    The schematic speaks of pins and the board of pads; the device's own
    <map> is what joins them (map.md), and a pin may answer several pads
    when one land is drawn as several islands."""
    by_name = {p.name: p for p in schematic.parts}
    by_component = {(c.library, c.name): c for c in components}
    out: dict = {}
    for net in schematic.nets:
        for segment in net.segments:
            for ref in segment.pinrefs:
                part = by_name.get(ref.inst)
                if part is None:
                    continue
                component = by_component.get((part.library or None, part.component))
                if component is None or not component.devices:
                    continue
                device = next((d for d in component.devices
                               if d.name == (part.device or "")), None)
                if device is None:
                    continue
                for m in device.maps:
                    if m.gate == (ref.gate or "") and m.pin == ref.pin:
                        for pad in m.pads:
                            out[(part.name, pad)] = net.name
    return out


def _sheet_name(source: Source, path: Path) -> str:
    """What KiCad calls this page — the root pages are named in the project
    file, a flattened sheet by the `Sheetname` of the symbol placing it."""
    for top in (source.settings.get("schematic") or {}).get("top_level_sheets") or []:
        if (path.parent / top.get("filename", "")).resolve() == path:
            return str(top.get("name") or path.stem)
    for tree in source.sheets.values():
        for sheet in sexpr.kids(tree, "sheet"):
            found = name = ""
            for prop in sexpr.kids(sheet, "property"):
                atoms = sexpr.atoms(prop)
                if len(atoms) < 2:
                    continue
                if str(atoms[0]) == "Sheetfile":
                    found = str(atoms[1])
                elif str(atoms[0]) == "Sheetname":
                    name = str(atoms[1])
            if found and (path.parent / found).resolve() == path:
                return name or path.stem
    return path.stem


def import_project(path: Path):
    source = read_source(path)
    symbols, components, pin_pads = library_conv.convert_libraries(
        list(source.sheets.values()), log)
    pairs = library_conv.footprint_pairs(list(source.sheets.values()), log)
    filed = library_conv.attach_devices(components, pin_pads, pairs,
                                         source.footprints, log)

    footprints = []
    for name, libraries in filed.items():
        reference = source.footprints.get(name)
        if reference is None:
            continue
        for library in sorted(libraries, key=lambda v: (v is None, v)):
            # library.md: a device names its footprint within its OWN
            # library, so a footprint named from two libraries is filed in
            # both — one object each, since each carries its library.
            footprints.append(replace(reference, library=library))

    schematic = build_schematic(source, components, symbols, log)
    layout = board_conv.convert_board(source.board, source.name,
                                       {n.name for n in schematic.nets}, log,
                                       _net_by_pad(schematic, components),
                                       source.settings)
    log(f"{source.name}: {len(source.sheets)} page(s), {len(schematic.parts)} parts, "
        f"{len(schematic.nets)} nets, {len(components)} components, "
        f"{len(footprints)} footprints; board: {len(layout.elements)} elements, "
        f"{len(layout.signals)} signals, stack {layout.stack}")
    # class.md: the pool is the project's, and a net names its class by a
    # `class` attr. KiCad states membership by name patterns instead.
    classes = board_conv.read_classes(source.settings, log, source.name)
    known = {c.name for c in classes}
    membership = board_conv.class_membership(
        source.settings, [n.name for n in schematic.nets], log, source.name)
    for net in schematic.nets:
        chosen = membership.get(net.name)
        if chosen in known:
            net.attrs.append(Attr("class", chosen))

    return Project(name=source.name, version=(1, 0), schematic=schematic,
                   symbols=symbols, footprints=footprints,
                   components=components, classes=classes, layouts=[layout])
