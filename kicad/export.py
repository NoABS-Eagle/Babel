"""IR `Project` -> KiCad project folder — conversion-kicad.md #что-получается
(the "В KiCad" pass). Board export and multi-page/module projects aren't
implemented yet — this covers a single-page (no modules) schematic-only
project, so it can be checked against a real Eagle-imported test project
before the rest is built.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from eagle.schematic_export import build_sheets
from ir import model3d
from ir.module_instance import expand_designator

from . import board_export
from . import footprint_export as fp_export
from . import library_export as lib_export
from . import schematic_export as sch_export
from .sexpr import write


def board_parts(project) -> dict:
    """designator -> the `Part` it names, **channels included**.

    A board is flat: it knows nothing of modules and calls a channel's part
    by its expanded name (`C101`, not `DCDC1`'s `C1`), so the schematic has
    to be flattened the same way to answer it — `expand_designator` is the
    one function that already does this everywhere else in the converter.
    Without the channel half, every part inside a module simply failed to
    resolve and went unplaced: 56 of step4's 72 elements were missing from
    the board, silently."""
    parts = {p.name: p for p in project.schematic.parts}
    modules = {m.name: m for m in project.modules}
    for mi in project.schematic.modinsts:
        module = modules.get(mi.module)
        if module is None:
            continue
        for p in module.schematic.parts:
            parts[expand_designator(p.name, mi.name, mi.offset)] = p
    return parts


def _footprint_of_element(project) -> dict:
    """element name -> the `Footprint` it places. element.md: the board
    names only the designator, and the schematic's part+device is what
    decides which footprint that designator wears."""
    parts = board_parts(project)
    components = {(c.library.lower(), c.name.lower()): c for c in project.components}
    footprints = {(f.library, f.name): f for f in project.footprints}
    by_name = {f.name: f for f in project.footprints}
    out = {}
    for name, part in parts.items():
        component = components.get((part.library.lower(), part.component.lower()))
        if component is None:
            continue
        device = next((d for d in component.devices if d.name == (part.device or "")), None)
        if device is None or not device.footprint:
            continue
        out[name] = (footprints.get((component.library, device.footprint))
                     or by_name.get(device.footprint))
    return {k: v for k, v in out.items() if v is not None}


def _nets_named_on_schematic(schematic, ports, parts_by_name,
                              components_by_key, symbols_by_key) -> set[str]:
    """Which nets of one schematic will carry their own name into KiCad.

    Three things state a name, and all three are DRAWN: a label the author
    put on the net, a power symbol sitting on it (KiCad reads the name off
    its `Value`), and — inside a module — the module's own port. Anything
    else is a net whose name the source itself never shows."""
    named = {p.name for p in ports or ()}
    for net in schematic.nets:
        if any(seg.labels for seg in net.segments):
            named.add(net.name)
        elif sch_export._named_by_power_symbol(net.name, net.segments, parts_by_name,
                                                components_by_key, symbols_by_key):
            named.add(net.name)
    return named


def _report_renamed_nets(project, channels, modules_by_name, components_by_key,
                          symbols_by_key, log) -> None:
    """Say out loud which board nets KiCad is going to rename.

    **Only a name the source SHOWS is carried over.** Where the author drew
    no label, the name (`N$5`, and any net the author renamed without
    displaying it) is one neither Eagle nor the IR puts on the drawing, so
    it is not worth adding a drawing element for; KiCad names such a net
    itself. The board still writes Eagle's name on that copper — the
    alternative, copper with no net at all, is worse in every way and DRC
    would say so immediately — so the two files disagree about what to
    call it until the first "Update PCB from Schematic".

    That is a real consequence, and conversion.md forbids losing it
    silently. Hence this report: it names every net it applies to, so a
    person who cares about one of them can go draw a label in the SOURCE,
    which is the only place the fix belongs."""
    root_parts = {p.name: p for p in project.schematic.parts}
    named = _nets_named_on_schematic(project.schematic, None, root_parts,
                                      components_by_key, symbols_by_key)
    per_module: dict[str, set[str]] = {}
    for module_name, module in modules_by_name.items():
        parts = {p.name: p for p in module.schematic.parts}
        per_module[module_name] = _nets_named_on_schematic(
            module.schematic, module.symbol.pins, parts, components_by_key, symbols_by_key)

    channel_module = {mi.name: module for module, insts in channels.items() for mi in insts}
    renamed = []
    for layout in project.layouts:
        for signal in layout.signals:
            if not signal.contactrefs:
                continue
            channel, sep, rest = signal.name.partition(":")
            module = channel_module.get(channel) if sep else None
            if module is None:
                if signal.name not in named:
                    renamed.append(signal.name)
            elif rest not in per_module.get(module, ()):
                renamed.append(signal.name)
    if renamed:
        shown = ", ".join(sorted(renamed)[:10])
        log(f"{len(renamed)} цепей не несут метки в исходнике — их имена KiCad назначит сам "
            f"(`Net-(IC1-SW)` и подобные), а медь на плате останется под именем Eagle: до "
            f"первого «Update PCB from Schematic» файлы зовут эти цепи по-разному. Чтобы имя "
            f"сохранилось, поставьте на цепь ярлык В ИСХОДНИКЕ: {shown}"
            f"{', …' if len(renamed) > 10 else ''}")


def _is_power(part, components_by_key, symbols_by_key) -> bool:
    component = components_by_key[(part.library.lower(), part.component.lower())]
    gate1 = symbols_by_key[(component.library, component.gates[0].symbol)]
    return lib_export.is_power_symbol(gate1)

_KICAD_SYM_VERSION = 20251024  # matches the ground-truth .kicad_sym header


def export_symbol_library(name: str, symbols, components, log) -> list:
    symbols_by_key = {(s.library, s.name): s for s in symbols if s.name is not None}
    node = ["kicad_symbol_lib", ["version", _KICAD_SYM_VERSION],
            ["generator", "babel"], ["generator_version", "10.0"]]
    for c in components:
        node.extend(lib_export.write_component(c, symbols_by_key, log))
    return node


def write_symbol_library(name: str, symbols, components, log, path: Path) -> None:
    write(export_symbol_library(name, symbols, components, log), path)


def write_sym_lib_table(libraries: list[str], path: Path) -> None:
    node = ["sym_lib_table", ["version", 7]]
    for lib in libraries:
        node.append(["lib", ["name", lib], ["type", "KiCad"],
                      ["uri", f"${{KIPRJMOD}}/{lib}.kicad_sym"], ["options", ""], ["descr", ""]])
    write(node, path)


FOOTPRINT_POOL_DIR = "footprints.pretty"
MODEL_POOL_DIR = "3dmodels"


def copy_3d_models(footprints, source_models_dir, out_dir: Path, log) -> dict[str, str]:
    """Copy every footprint's STEP file into the project and return
    `footprint name -> the path to write in its `(model ...)``.

    model3d.md keeps the placement numbers and NOT the file name — the
    file is found by convention, `<footprint>[_<key>].step|.stp` in the
    source project's own model folder — so the exporter has to be told
    where that folder is; nothing in the IR can answer it. Same shape as
    the Eagle export's `_copy_3d_models`, and the same best-effort rule:
    a missing file costs the model, never the footprint.

    `${KIPRJMOD}` is KiCad's own project-relative prefix (ground truth:
    `testData/kicad/2r` and `byte` both reference their models that way),
    which is what makes the exported project self-contained."""
    with_models = [f for f in footprints if f.models]
    if not with_models:
        return {}
    if source_models_dir is None:
        log(f"3D models: {len(with_models)} footprint(s) carry placement metadata, but no source "
            f"model folder was given — the board goes out without models")
        return {}

    pool = out_dir / MODEL_POOL_DIR
    paths: dict[str, str] = {}
    missing = []
    for fp in with_models:
        # A KiCad footprint holds ONE model set and cannot switch on an
        # attribute value, so only the unconditional record travels.
        if not any(m.key is None for m in fp.models):
            continue
        if fp.name in paths:
            continue
        src = model3d.find_file(source_models_dir, fp.name, None)
        if src is None:
            missing.append(fp.name)
            continue
        pool.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, pool / src.name)
        paths[fp.name] = f"${{KIPRJMOD}}/{MODEL_POOL_DIR}/{src.name}"

    keyed = sorted({f.name for f in with_models if any(m.key is not None for m in f.models)})
    if keyed:
        log(f"{len(keyed)} footprint(s) carry keyed 3D models — a KiCad footprint holds one "
            f"model set and cannot switch on an attribute value, so only the unconditional one "
            f"travels: {', '.join(keyed[:6])}{', …' if len(keyed) > 6 else ''}")
    if missing:
        log(f"{len(missing)} footprint(s) declare a 3D model whose .step/.stp is not in "
            f"{source_models_dir} — they carry over without it: "
            f"{', '.join(sorted(missing)[:6])}{', …' if len(missing) > 6 else ''}")
    if paths:
        log(f"3D models: {len(paths)} file(s) copied into {MODEL_POOL_DIR}/")
    return paths


def write_fp_lib_table(nickname: str, path: Path) -> None:
    """conversion-kicad.md #библиотеки: footprints go the opposite way from
    symbols — **one pool for the whole project**, not a library per IR
    library. KiCad has no library-level device to hang them off, and the
    schematic already names each one `<nickname>:<footprint>`."""
    write(["fp_lib_table", ["version", 7],
            ["lib", ["name", nickname], ["type", "KiCad"],
             ["uri", f"${{KIPRJMOD}}/{FOOTPRINT_POOL_DIR}"], ["options", ""], ["descr", ""]]],
           path)


# What a filename may not hold on Windows or POSIX. Everything else —
# parentheses, `+`, spaces — is legal and stays untouched.
_FILENAME_FORBIDDEN = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def write_footprint_pool(footprints, out_dir: Path, log, hole_drills=(),
                          model_paths=None) -> int:
    """One `.kicad_mod` per footprint. Names collide across IR libraries
    far more readily than symbols do (every library has its own `R0603`);
    the first one wins the name and the rest are logged, exactly as
    conversion-kicad.md's own #библиотеки says — "берётся первый корпус".

    **The file name IS the footprint name, verbatim.** A `.pretty` library
    takes the name from the file and nothing else — the `(footprint "...")`
    inside is ignored on read, and no unescaping happens either (ground
    truth: `kicad-cli fp export svg --fp` finds `DO-214AB(SMC)` only when
    the file is called that, and reads `X%3AY.kicad_mod` as the literal
    name `X%3AY`). So a name quietly folded to something filesystem-safe
    is a name the schematic's own `Footprint` field can no longer resolve
    — which is exactly how `DO-214AB(SMC)` became a file called
    `DO-214AB_SMC_` that KiCad refused to find. A name a filesystem cannot
    express is refused out loud instead of renamed."""
    pool = out_dir / FOOTPRINT_POOL_DIR
    pool.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    for fp in footprints:
        origin = fp.library or "?"
        if fp.name in seen:
            if seen[fp.name] != origin:
                log(f"footprint {fp.name!r}: also defined in library {origin!r} — kept the one "
                    f"from {seen[fp.name]!r} (one flat pool, conversion-kicad.md #библиотеки)")
            continue
        bad = sorted(_FILENAME_FORBIDDEN & set(fp.name))
        if bad:
            log(f"footprint {fp.name!r} (library {origin!r}): the name holds {bad!r}, which a "
                f"file name cannot carry — and a KiCad footprint IS its file name. Not written; "
                f"rename it in the source project")
            continue
        seen[fp.name] = origin
        write(fp_export.write_footprint(fp, log, model_paths), pool / f"{fp.name}.kicad_mod")

    # A bare hole is a footprint on this board (hole.md has one, KiCad has
    # not), so the pool has to hold it too — otherwise its `lib_id` names a
    # library that isn't there. One per drill diameter, board_export's own
    # naming.
    for drill in sorted(set(hole_drills)):
        name = board_export.hole_footprint_name(drill)
        if name in seen:
            continue
        seen[name] = "hole.md"
        write(board_export.write_hole_library_footprint(drill), pool / f"{name}.kicad_mod")
    return len(seen)


def _design_settings(rules) -> dict:
    """rules.md's six numbers -> KiCad's own constraint names.

    They live in the `.kicad_pro`, not in the `.kicad_pcb` — which is why
    leaving that file a skeleton left KiCad applying its built-in defaults
    and drawing every pour with a gap nobody asked for. One-to-one, no
    invention; everything KiCad also constrains and rules.md does not
    model stays absent, so KiCad keeps deciding it.

    The net class matters as much as the constraints: a zone's gap to
    other nets comes from `clearance` THERE, not from the minimum."""
    mm = lambda um: um / 1000
    return {
        "rules": {
            "min_clearance": mm(rules.clearance),
            "min_copper_edge_clearance": mm(rules.edge_clearance),
            "min_track_width": mm(rules.min_width),
            "min_through_hole_diameter": mm(rules.min_drill),
            "min_annular_width": mm(rules.min_annular),
            "min_hole_to_hole": mm(rules.min_drill_web),
        },
    }


def _net_settings(rules, project) -> dict:
    """`Default` from the board's own floor, then one entry per IR
    `<class>`, then the name-based assignments.

    **Only the three numbers class.md actually keeps travel** —
    `width`/`clearance`/`drill` land on KiCad's `track_width`/`clearance`/
    `via_drill`, and everything else in a KiCad class (diff pairs, microvia
    sizes, colours) is left to KiCad's own defaults rather than invented.
    A class whose rules the IR never held would otherwise arrive looking
    authoritative and be wrong.

    `netclass_patterns` assigns BY NAME, and that is written for the sake
    of the BOARD: its copper carries the IR's own net names, so the classes
    are right there the moment the file opens, before any "Update PCB from
    Schematic". The schematic gets its membership the other way, from the
    directives `schematic_export.write_netclass_flag` draws — the only
    channel that works for a net the source never named."""
    classes = [{"name": "Default",
                 "clearance": rules.clearance / 1000,
                 "track_width": rules.min_width / 1000}]
    for c in project.classes:
        entry = {"name": c.name}
        if c.width is not None:
            entry["track_width"] = c.width / 1000
        if c.clearance is not None:
            entry["clearance"] = c.clearance / 1000
        if c.drill is not None:
            entry["via_drill"] = c.drill / 1000
        classes.append(entry)

    channels = {mi.name for mi in project.schematic.modinsts}
    modules = {m.name: m for m in project.modules}
    patterns = []
    seen = set()

    def add(name: str, class_name: str) -> None:
        if (name, class_name) not in seen:
            seen.add((name, class_name))
            patterns.append({"netclass": class_name, "pattern": name})

    for net in project.schematic.nets:
        if net.attr("class"):
            add(net.name, net.attr("class"))
    for mi in project.schematic.modinsts:
        module = modules.get(mi.module)
        if module is None:
            continue
        for net in module.schematic.nets:
            if net.attr("class"):
                # The board calls a channel's own net `/CHANNEL/NAME`, the
                # same spelling board_export.kicad_net_name produces.
                add(f"/{mi.name}/{net.name}", net.attr("class"))
    out = {"classes": classes}
    if patterns:
        out["netclass_patterns"] = patterns
    return out


_DRAWING_DEFAULTS = {
    "dashed_lines_dash_length_ratio": 12.0,
    "dashed_lines_gap_length_ratio": 3.0,
    "default_line_thickness": 6.0,
    "default_text_size": 50.0,
    "hop_over_size_choice": 0,
    "junction_size_choice": 3,
    "overbar_offset_ratio": 1.23,
    "pin_symbol_size": 25.0,
}
"""How the schematic is DRAWN — none of it conversion, all of it needed.

**A key absent from the `.kicad_pro` is not the same as KiCad's own
default.** Leaving this block out made every junction dot render at zero
size: the dots were all there in the file (exactly the 4 and 9 the Eagle
source itself carries), KiCad drew provisional ones while a symbol was
being dragged, then re-rendered them into nothing on drop — which is how
the user found it.

These are the values two unrelated real projects agree on
(`testData/Eagle/tolmach/.../kicad` and `testData/kicad/pic_programmer`);
the keys where they differ are per-project taste and stay absent."""


def _minimal_project_json(name: str, pages: list[dict], sheets: list[tuple[str, str]],
                           rules=None, project=None) -> dict:
    """A best-effort skeleton, not a full settings mirror — KiCad fills
    in its own defaults for anything absent. The schematic content
    itself (`.kicad_sch`) is where the real conversion work lives.

    Two fields are NOT optional once the project has sub-sheets, and they
    are separate registries (ground truth: every multi-sheet project in
    `testData/kicad`): `schematic.top_level_sheets` names the root file,
    and the top-level `sheets` is the flat inventory of EVERY sheet
    occurrence, root included, in page order."""
    return {
        "board": {"design_settings": _design_settings(rules)} if rules else {},
        "boards": [], "cvpcb": {}, "erc": {},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "net_settings": _net_settings(rules, project) if rules else {}, "pcbnew": {},
        "schematic": {
            "drawing": _DRAWING_DEFAULTS,
            "top_level_sheets": [
                {"filename": p["file"], "name": p["name"], "uuid": p["uuid"]} for p in pages]},
        "sheets": [[uuid, sheet_name] for uuid, sheet_name in sheets],
        "text_variables": {},
    }


def write_project_json(name: str, pages: list[dict], sheets: list[tuple[str, str]], path: Path,
                        rules=None, project=None) -> None:
    path.write_text(json.dumps(_minimal_project_json(name, pages, sheets, rules, project), indent=2),
                    encoding="utf-8", newline="\n")


def _module_page(schematic) -> dict:
    """A module's own schematic as one `build_sheets`-shaped bucket. It
    cannot go through `build_sheets` itself: module.md gives a module
    exactly one page and Eagle draws no frame inside one, so there is
    nothing to tile by — the page is simply everything, and its bbox is
    what the content spans."""
    xs, ys = [], []
    for ci in schematic.instances:
        xs.append(ci.x)
        ys.append(ci.y)
    for net in schematic.nets:
        for seg in net.segments:
            for ln in seg.lines:
                xs += [ln.x1, ln.x2]
                ys += [ln.y1, ln.y2]
            for lbl in seg.labels:
                xs.append(lbl.x)
                ys.append(lbl.y)
    if not xs:
        raise ValueError("module schematic is empty — nothing to put on its KiCad page")
    nets = {}
    for net in schematic.nets:
        nets[net.name] = (net, list(net.segments))
    return {"instances": list(schematic.instances), "modinsts": [],
            "graphics": list(schematic.graphics), "nets": nets,
            "frame_part": None, "bbox": (min(xs), min(ys), max(xs), max(ys))}


def _sanitize(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


def write_project(project, path: Path, log, source_models_dir=None) -> Path:
    """The project's `.kicad_sch` files, one symbol library per IR library,
    a `sym-lib-table` and a `.kicad_pro`.

    conversion-kicad.md #страницы: **a module is exactly one sheet**, and
    each of its channels is one `(sheet ...)` placement of that same file —
    which is precisely KiCad's own multichannel mechanism, so the two-level
    IR model carries over without flattening. Multi-page top levels and the
    board are still ahead; `build_sheets` raises on the first, matching the
    Eagle export's own rule."""
    path = Path(path)
    out_dir = path.parent / path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols_by_key = {(s.library, s.name): s for s in project.symbols if s.name is not None}
    components_by_key = {(c.library.lower(), c.name.lower()): c for c in project.components}

    sheets = build_sheets(project.schematic, symbols_by_key, components_by_key, log)

    # **Every frame is its own ROOT page** — KiCad 10's flat multi-root, not
    # a synthesized parent sheet holding them all. conversion-kicad.md
    # #страницы cuts the schematic into files by frames and says nothing of
    # a page above them, and Eagle's own sheets are peers, so inventing a
    # root would invent a hierarchy level the source does not have.
    pages = []
    for index, sheet in enumerate(sheets, start=1):
        title = next((a.value for a in sheet["frame_part"].attrs if a.name == "sheet"), None)
        pages.append({
            "sheet": sheet,
            "uuid": str(uuid.uuid4()),
            "file": f"{path.stem}.kicad_sch" if index == 1
                    else f"{path.stem}-{index}.kicad_sch",
            # The frame's own `sheet` attribute is Eagle's sheet description,
            # already numbered by its author ("2. MCU") — a better page name
            # than anything synthesized, and what the navigator shows.
            "name": title or (path.stem if index == 1 else f"{path.stem}-{index}"),
            "number": index,
        })

    modules_by_name = {m.name: m for m in project.modules}

    # One file per MODULE, one sheet node per CHANNEL — the file is drawn
    # once and placed N times, so every symbol inside it needs one
    # occurrence per channel (module-instance.md's own expanded names).
    # A channel belongs to the PAGE its frame encloses, so its path is
    # `/<that page>/<sheet>`, never a single fixed root.
    channels: dict[str, list] = {}
    sheet_uuids: dict[str, str] = {}
    channel_page: dict[str, dict] = {}
    channel_number: dict[str, int] = {}
    next_number = len(pages) + 1
    for page in pages:
        for mi in page["sheet"]["modinsts"]:
            if mi.module not in modules_by_name:
                raise ValueError(f"channel {mi.name!r} names unknown module {mi.module!r}")
            sheet_uuids[mi.name] = str(uuid.uuid4())
            channel_page[mi.name] = page
            channel_number[mi.name] = next_number
            next_number += 1
            channels.setdefault(mi.module, []).append(mi)

    module_files = {name: f"{_sanitize(name)}.kicad_sch" for name in channels}
    taken = {p["file"] for p in pages}
    for name, fname in module_files.items():
        if fname in taken:
            module_files[name] = f"{_sanitize(name)}_module.kicad_sch"

    # Which nets straddle pages — those need their name written on every
    # page they touch (see write_page's own reasoning).
    pages_of_net: dict[str, int] = {}
    for page in pages:
        for name in page["sheet"]["nets"]:
            pages_of_net[name] = pages_of_net.get(name, 0) + 1
    multi_page_nets = {n for n, count in pages_of_net.items() if count > 1}


    # designator -> (unit, KIID path of its symbol), filled in as the pages
    # are written and handed to the board so each footprint can name the
    # symbol it stands for. The unit rides along only to pick the first
    # section of a multi-unit part; see write_instance's own reasoning.
    collected_paths: dict[str, tuple[int, str]] = {}

    root_parts = {p.name: p for p in project.schematic.parts}
    for page in pages:
        sheet = page["sheet"]
        origin = (sheet["bbox"][0] - sch_export._PAGE_MARGIN,
                  sheet["bbox"][3] + sch_export._PAGE_MARGIN)
        page_path = f"/{page['uuid']}"
        sheet_nodes = []
        for mi in sheet["modinsts"]:
            module = modules_by_name[mi.module]
            sheet_nodes.append(sch_export.write_sheet(
                mi, module, sheet_uuids[mi.name], module_files[mi.module],
                channel_number[mi.name], project.name, page_path, origin, log))
            sheet_nodes += sch_export.write_port_stubs(
                mi, module, sheet["nets"], origin, log)

        node = sch_export.write_page(
            sheet, project, symbols_by_key, components_by_key, footprint_pool=path.stem, log=log,
            parts_by_name=root_parts, page_uuid=page["uuid"],
            occurrences_for=lambda part, _p=page_path: [(_p, lib_export.power_designator(
                part.name, _is_power(part, components_by_key, symbols_by_key)))],
            extra_nodes=sheet_nodes, page_number=page["number"],
            must_name=multi_page_nets, where=f"на листе {page['name']!r}",
            paths_out=collected_paths)
        write(node, out_dir / page["file"])

    for module_name, instances in channels.items():
        module = modules_by_name[module_name]
        parts = {p.name: p for p in module.schematic.parts}

        def occurrences_for(part, _insts=instances):
            return [(f"/{channel_page[mi.name]['uuid']}/{sheet_uuids[mi.name]}",
                      lib_export.power_designator(
                          expand_designator(part.name, mi.name, mi.offset),
                          _is_power(part, components_by_key, symbols_by_key)))
                     for mi in _insts]

        node = sch_export.write_page(
            _module_page(module.schematic), project, symbols_by_key, components_by_key,
            footprint_pool=path.stem, log=log, parts_by_name=parts,
            page_uuid=str(uuid.uuid4()), occurrences_for=occurrences_for,
            local_labels=True, ports=module.symbol.pins, root_page=False,
            where=f"в модуле {module_name!r}", paths_out=collected_paths)
        write(node, out_dir / module_files[module_name])

    by_lib: dict[str, tuple[list, list]] = {}
    for s in project.symbols:
        if s.name is not None:
            by_lib.setdefault(s.library, ([], []))[0].append(s)
    for c in project.components:
        by_lib.setdefault(c.library, ([], []))[1].append(c)
    for lib_name, (syms, comps) in by_lib.items():
        write_symbol_library(lib_name, syms, comps, log, out_dir / f"{lib_name}.kicad_sym")
    write_sym_lib_table(sorted(by_lib), out_dir / "sym-lib-table")

    hole_drills = [h.drill for layout in project.layouts for h in layout.holes]
    model_paths = copy_3d_models(project.footprints, source_models_dir, out_dir, log)
    count = write_footprint_pool(project.footprints, out_dir, log, hole_drills, model_paths)
    write_fp_lib_table(path.stem, out_dir / "fp-lib-table")
    log(f"footprint pool: {count} .kicad_mod written to {FOOTPRINT_POOL_DIR}/")

    if project.layouts:
        if len(project.layouts) > 1:
            raise ValueError(f"kicad export: {len(project.layouts)} layouts — a KiCad project "
                              f"holds exactly one board")
        _report_renamed_nets(project, channels, modules_by_name,
                              components_by_key, symbols_by_key, log)
        board = board_export.write_board(
            project.layouts[0], project, _footprint_of_element(project), path.stem, log,
            {ref: board_path for ref, (_unit, board_path) in collected_paths.items()},
            model_paths)
        write(board, out_dir / f"{path.stem}.kicad_pcb")

    registry = ([(p["uuid"], p["name"]) for p in pages]
                + [(sheet_uuids[name], name) for name in sheet_uuids])
    rules = project.layouts[0].rules if project.layouts else None
    write_project_json(path.stem, pages, registry, out_dir / f"{path.stem}.kicad_pro",
                        rules, project)
    return out_dir


# Kept under its old name: the single-page entry point every caller still uses.
write_single_page_project = write_project
