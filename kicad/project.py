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
from dataclasses import dataclass, field
from pathlib import Path

from import_log import log

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
    """Footprint name -> its `.kicad_mod`, the default placeholder layout
    and nothing else (conversion-kicad.md #что-приготовить)."""


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
    )


def import_project(path: Path):
    source = read_source(path)
    log(f"{source.name}: {len(source.sheets)} sheet(s), "
        f"{len(sexpr.kids(source.board, 'footprint'))} footprints on the board, "
        f"{len(source.footprint_lib)} footprints in the project library")
    raise SystemExit(
        "KiCad: the project reads and passes its preconditions; conversion "
        "itself is not written yet.")
