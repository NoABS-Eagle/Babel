"""KiCad full PROJECT (.kicad_pro + .kicad_pcb + .kicad_sch hierarchy) -> IR
XML converter.

Project-level import — see progress.md "KiCad-парсер/импортёр" for why this
supersedes library-only import as the primary path: builds Eagle-like device
libraries EMPIRICALLY from a real project.

Pipeline:
  1. Which footprint goes with which symbol is read directly off the
     SCHEMATIC, not the board: each placed SchematicSymbol instance carries
     its own `Footprint` property (the same field KiCad itself uses for
     "Update PCB from Schematic"). Confirmed on a real re-exported project
     (testData/phil.zip, generic `Device:C` placed 12 times): the per-
     instance Footprint property already varies (3 instances say
     `Capacitor_SMD:C_0805_2012Metric`, the rest say `..._0603...`) — the
     full generic-merge picture is available from the schematic ALONE, no
     board path/UUID matching needed at all (an earlier version of this
     importer used the board for this; dropped once this was confirmed).
  2. Both the symbol's and the footprint's actual GEOMETRY are resolved
     fresh from their real libraries — sym-lib-table / fp-lib-table — never
     trusted from whatever's cached/embedded in the schematic or board (see
     precondition 3 below for why).
  3. A schematic symbol that resolves to >1 distinct footprint across its
     placed instances becomes one <component> with several <footprint>
     variants (IR already supports this natively, see ir_schema.md "Корпус
     <footprint>").
  4. Value (and any other per-instance-only data) is never baked as a
     device-level attribute — only the symbol's own resolved-from-library
     Fields are used for attrs/placeholders, never the per-instance
     schematicSymbol properties. This extends the same "Value/Comment is
     per-instance schematic data, not device-level" principle used for
     Altium IntLib and KiCad library-only generic symbols to project import
     as a whole.

The board (.kicad_pcb) is still read, but for exactly one thing: the raw
bytes of embedded 3D models (see precondition 1) — that data only exists on
the board (KiCad's "embed file" feature is board-level), and a re-exported
footprint library's `kicad-embed://...` reference is dangling without it
(confirmed: testData/phil.zip's exported Library.pretty/*.kicad_mod files
carry the embed-style path string but no `embedded_files` block of their
own — only 222 lines, real embedded data needs thousands).

Three preconditions on the source project (operator-driven, not auto-
detected — we do not try to guess our way around a broken project):
  - 3D models must be embedded into the board (KiCad's own "embed file"
    feature) before export — env-var paths like ${KICAD6_3DMODEL_DIR}/... are
    not reliably resolvable across machines.
  - The project should be handed over via KiCad's own "Archive Project" zip —
    confirmed (testData/video.zip) to contain exactly one *.kicad_pro at its
    root alongside the project's own local libraries, giving an unambiguous
    entry point.
  - sym-lib-table and fp-lib-table must actually resolve every symbol/
    footprint used. A symbol/footprint cached in the schematic/board is
    always a full inline snapshot that may silently diverge from the real
    library (KiCad's "Edit Symbol.../Edit Footprint..." let you hand-edit
    ONE placement without touching the library, with no visible marker for
    it afterwards on the footprint side, and only the easily-ignored
    `lib_name` token on the symbol side). Confirmed by direct experiment:
    editing a footprint in-place on a board, then breaking the library it
    came from, then running KiCad's own "Export Footprints to New
    Library..." — while the library still resolved, export silently
    discarded the on-board edit and produced the clean original; only once
    the library path was ALSO broken did it fall back to the edited board
    copy. KiCad's own tooling treats the library as truth and the
    schematic/board as a last resort — this importer does the same, but
    stricter: if a symbol or footprint's library doesn't resolve, conversion
    ABORTS with an error naming exactly what's missing, rather than silently
    skipping the component or substituting a possibly hand-edited cached
    copy — the project archive is required to carry every library it uses;
    finding a missing one is the user's job, not ours to guess at. Run
    "Export Footprints/Symbols to New Library..."
    and fix sym-lib-table/fp-lib-table beforehand if needed — and note that
    a real project-exported library commonly does NOT relink the schematic's
    own `lib_id` references to the new nickname (confirmed on phil.zip), so
    resolution always falls back to a by-name search across every
    registered library, not just nickname matching.
"""
import base64
import copy
import math
import fnmatch
import json
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

import zstandard
from kiutils.board import Board
from kiutils.schematic import Schematic
from kiutils.libraries import LibTable
from kiutils.utils import sexpr as _kiutils_sexpr

from babel.ir_util import sanitize_filename, clean_attr_name
from babel import import_log
from babel.kicad_parser import (
    _group_gates, _build_gate_symbol, _emit_property_placeholder, _is_hidden,
    _convert_footprint, _pin_mapping, _kicad_blank, _kicad_overbar_to_eagle,
    _resolve_footprint_path, _read_footprint, _resolve_symbol,
    _um, _f, _rot_fp, _text_size_um, _stroke_width_um, _arc_params,
    _unit_id_to_gate_letter, _KICAD_RESERVED_PROP_KEYS, _align,
    _is_erc_power_source, _contains_hide_yes,
)
from babel.kicad_schematic import (_pt, _mirror_and_angle, _abs_pin_pos_mm,
                                   _build_nets, field_to_kicad,
                                   field_from_kicad)
from babel.svg_renderer import _bounds

_EMBED_PREFIX = 'kicad-embed://'


# ---------------------------------------------------------------------------
# Embedded 3D models (KiCad 9 "embed file" feature)
# ---------------------------------------------------------------------------

def _decode_embedded_files(text):
    """{name: bytes} for every (embedded_files (file (name ..) (type model)
    (data |..|))) entry in a .kicad_pcb's raw text.

    kiutils 1.4.8 has no support at all for the `embedded_files` token (no
    hits anywhere in board.py/footprint.py) — same kind of read gap as the
    Property `hide` workaround in kicad_parser.py, so this re-walks the raw
    S-expression instead. Confirmed format empirically against
    testData/video/video.kicad_pcb (23 real embedded models, both .step and
    .wrl): `data`'s value is base64 of a zstd-compressed blob — raw bytes
    start with 28 B5 2F FD, the zstd frame magic number (base64 "KLUv...").
    kiutils' sexpr tokenizer doesn't understand the `|...|` delimiter either:
    it comes back as a list of whitespace-split fragments with the leading
    `|` glued to the first one and the trailing `|` glued to the last —
    `''.join(...).strip('|')` reassembles the original base64 string.
    """
    tree = _kiutils_sexpr.parse_sexp(text)
    dctx = zstandard.ZstdDecompressor()
    files = {}
    for item in tree[1:]:
        if not (isinstance(item, list) and item and item[0] == 'embedded_files'):
            continue
        for f in item[1:]:
            if not (isinstance(f, list) and f and f[0] == 'file'):
                continue
            name = ftype = data_tokens = None
            for sub in f[1:]:
                if sub[0] == 'name':
                    name = sub[1]
                elif sub[0] == 'type':
                    ftype = sub[1]
                elif sub[0] == 'data':
                    data_tokens = sub[1:]
            if ftype != 'model' or not name or not data_tokens:
                continue
            raw = ''.join(data_tokens).strip('|')
            decoded = base64.b64decode(raw)
            files[name] = dctx.decompress(decoded, max_output_size=200_000_000)
    return files


def _extract_embedded_models(pcb_path, out_dir):
    """Decode every embedded 3D model from pcb_path into out_dir, named as
    stored. Returns the set of names written — empty if the project's models
    weren't embedded (embedding is an operator precondition, not optional
    auto-detected behaviour — see module docstring).

    A project with NO .kicad_pcb at all is a legitimate schematic-only
    project (our own kicad_project_exporter emits exactly that — KiCad
    creates the board lazily on first pcbnew open), not a broken archive:
    nothing to extract, empty set.
    """
    pcb_path = Path(pcb_path)
    if not pcb_path.exists():
        return set()
    text = pcb_path.read_text(encoding='utf-8')
    files = _decode_embedded_files(text)
    if files:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (out_dir / name).write_bytes(data)
    return set(files)


def _copy_model3d(fp, fp_el, embedded_dir, out_models_dir, fp_name):
    """Copy a footprint's 3D model to out_models_dir, renamed to match the
    footprint (sidecar convention all exporters expect — see
    ir_util.resolve_model3d_file). Embedded-only: external/env-var paths
    (${KICAD6_3DMODEL_DIR}/...) point at libraries that may not exist on this
    machine at all, so they're not handled — see module docstring. `fp` here
    is the RESOLVED LIBRARY footprint (see convert_project) — its model path
    is the same `kicad-embed://NAME` string the board copy had (export
    doesn't rename the model), but the actual bytes only exist in
    `embedded_dir` (extracted from the board, the one place that data lives).
    """
    if not fp.models or fp_el.find('model3d') is None:
        return
    path = fp.models[0].path or ''
    if not path.startswith(_EMBED_PREFIX):
        print(f'  ! {fp_name}: 3D model path "{path}" is not embedded — '
              f'embed 3D models in KiCad before exporting the project (see progress.md)')
        return
    name = path[len(_EMBED_PREFIX):]
    src = (embedded_dir / name) if embedded_dir else None
    if src is None or not src.exists():
        print(f'  ! {fp_name}: embedded model "{name}" not found among extracted files')
        return
    out_models_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out_models_dir / f'{sanitize_filename(fp_name)}{src.suffix}')


# ---------------------------------------------------------------------------
# Supply locality (KiCad 10 `(power local)`) — raw s-expr side-channel
# ---------------------------------------------------------------------------

def _power_locality(sch_path):
    """{lib_symbols cache entry name: 'local'|'global'} for one .kicad_sch.

    KiCad 10 supply locality lives on the FILE's lib_symbols cache copy
    (`(power local)` vs `(power global)`), with the placed symbol pointing
    at a forked copy via `(lib_name "GND_1")` — confirmed on ground truth
    (user-made local #PWR048, testData/multichannel/channel_strip.kicad_sch,
    KiCad 10.0 / version 20260306). kiutils 1.4.8 SILENTLY DROPS the
    argument (both variants read back as bare isPower=True; its writer even
    emits argument-less `(power)`) — the confirmed-loss trigger from
    decisions.md «kiutils — deprecated», hence this raw re-walk of the
    s-expression, same pattern as _decode_embedded_files. A bare `(power)`
    (pre-10 writers) means global — the only semantics that existed.
    """
    tree = _kiutils_sexpr.parse_sexp(Path(sch_path).read_text(encoding='utf-8'))
    out = {}
    for item in tree[1:]:
        if not (isinstance(item, list) and item and item[0] == 'lib_symbols'):
            continue
        for s in item[1:]:
            if not (isinstance(s, list) and len(s) > 1 and s[0] == 'symbol'):
                continue
            for sub in s[2:]:
                if isinstance(sub, list) and sub and sub[0] == 'power':
                    out[s[1]] = sub[1] if len(sub) > 1 else 'global'
                    break
    return out


def _is_local_supply(sym, locality):
    """True if this PLACED symbol resolves to a `(power local)` cache copy.
    Lookup by the instance's own cache pointer: `lib_name` when the copy is
    forked (the ground-truth case), the full lib_id / bare entry name
    otherwise (different writers name unforked cache entries differently)."""
    for key in (getattr(sym, 'libName', None),
                f'{sym.libraryNickname}:{sym.entryName}' if sym.libraryNickname else None,
                sym.entryName):
        if key and key in locality:
            return locality[key] == 'local'
    return False


# ---------------------------------------------------------------------------
# Schematic hierarchy
# ---------------------------------------------------------------------------

def _load_schematic_tree(top_sch_path):
    """{file_path: Schematic} for top_sch_path + every sub-sheet transitively
    reachable from it, deduplicated by file (a sheet instantiated N times,
    e.g. multichannel's channel_strip.kicad_sch x4, is loaded once — and
    per-instance Reference is the only thing that varies across repeated
    instantiations of the same sub-sheet; Footprint/Value and everything
    else on SchematicSymbol.properties does not, so reading each file once
    is enough — see module docstring point 1).
    """
    top_sch_path = Path(top_sch_path)
    out = {}
    seen = set()
    queue = [top_sch_path]
    while queue:
        p = queue.pop()
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        out[p] = Schematic.from_file(str(p), encoding='utf-8')
        for sheet in out[p].sheets:
            queue.append(p.parent / sheet.fileName.value)
    return out


def _detect_pages(pro_path, project_dir):
    """[(path, Schematic), ...] in page order — this project's own
    independent top-level pages.

    Real ground truth (testData/vimdrones.zip) overturned the earlier
    assumption (made on testData/pic_programmer.zip) that a sheet
    instantiated exactly once is "just a page": KiCad actually has a
    SEPARATE, unambiguous mechanism for flat multi-page that needs no
    instantiation-count guessing at all — the project file's own
    `schematic.top_level_sheets` list, each entry its OWN independent root
    schematic (`sheet_instances` with `path "/"`, not nested under
    anything), cross-page connectivity via plain `global_label` (no
    sheet-pin/hierarchical_label machinery exists at all here — confirmed,
    testData/vimdrones.zip has zero `(sheet)`/`hierarchical_label` tokens
    in either file, 142 global labels, 0 local).

    Hierarchy is never flattened — but a DEPTH-1 `(sheet)` symbol is no
    longer a reject: it becomes an IR <module> (ir_schema.md "Модуль
    (design block + иерархия)") — see _collect_modules, which also hard-
    rejects anything nested deeper than one level. This function itself no
    longer looks at sch.sheets at all.
    """
    pro_data = json.loads(Path(pro_path).read_text(encoding='utf-8'))
    top_level = (pro_data.get('schematic') or {}).get('top_level_sheets') or []

    if not top_level:
        sch_path = project_dir / f'{pro_path.stem}.kicad_sch'
        entries = [{'filename': sch_path.name}]
    else:
        entries = top_level

    pages = []
    for entry in entries:
        sch_path = project_dir / entry['filename']
        sch = Schematic.from_file(str(sch_path), encoding='utf-8')
        page_num = sch.sheetInstances[0].page if sch.sheetInstances else None
        pages.append((page_num, sch_path, sch))

    def _page_key(p):
        try:
            return int(p[0])
        except (TypeError, ValueError):
            return 1 << 30
    pages.sort(key=_page_key)
    return [(path, sch) for _, path, sch in pages]


# ---------------------------------------------------------------------------
# Page frame (paper + title_block -> ir_schema.md "Frame" component)
# ---------------------------------------------------------------------------

# KiCad GlobalLabel/HierarchicalLabel `shape` -> IR <label> `style`
# (ir_schema.md "<label>" — presentation only, not electrical). LocalLabel
# (plain `(label ...)`) has no `shape` field at all — always plain text,
# handled by the caller's `getattr(l, 'shape', None)` falling through to
# the dict's own default ('crummy') rather than needing its own branch.
# `tri_state` has no direct equivalent among our 5 styles — maps to
# `bidir` (closest visually) per the user, rather than inventing a 6th.
_LABEL_STYLE = {
    'input': 'input', 'output': 'output', 'bidirectional': 'bidir',
    'passive': 'passive', 'tri_state': 'bidir',
}

_GAP_MM = 12.7   # 0.5" — same horizontal-row gap as ir_schema.md "Импорт страниц источника"

# ISO sizes in PORTRAIT (narrow-first) mm — KiCad's own default for
# schematics is landscape (portrait=False), so _paper_dims_mm swaps these
# back unless the file says otherwise. kiutils has no dimension table of
# its own (PageSettings only carries the bare size name).
_ISO_PAPER_MM = {
    'A5': (148, 210), 'A4': (210, 297), 'A3': (297, 420),
    'A2': (420, 594), 'A1': (594, 841), 'A0': (841, 1189),
}


def _paper_dims_mm(paper):
    """kiutils PageSettings -> (width_mm, height_mm), oriented per `portrait`.

    Confirmed against real data (testData/pic_programmer.zip): paperSize
    "A4", portrait=False, and the page's own placed symbols span up to
    x=274.32 — past A4's SHORT edge (210mm), only consistent with the LONG
    edge (297mm) being the width, i.e. landscape-by-default.
    """
    if paper.paperSize == 'User' and paper.width and paper.height:
        # "User" stores the LITERAL (width height) — orientation is baked
        # into the numbers, the portrait flag applies to named sizes only
        # (ground truth: our own exporter writes (paper "User" 260.35
        # 179.07) for a landscape page, and KiCad renders it 260-wide).
        return (paper.width, paper.height)
    w, h = _ISO_PAPER_MM.get(paper.paperSize, _ISO_PAPER_MM['A4'])
    return (h, w) if not paper.portrait else (w, h)


def _frame_component_name(width_mm, height_mm):
    return f'Frame_{width_mm:g}x{height_mm:g}mm'


def _build_frame_component(width_mm, height_mm, pool, symbols_el, proj_el):
    """Get-or-create the shared "Frame" <component>+<symbol> for one paper
    size (ir_schema.md "Frame" — a component without a footprint, exactly
    one rectangle on the `FRAME` layer). Reused across every page
    that shares this size; per-page Title/Date/Rev/Company are NOT baked in
    here — see the instance-building loop for why (real per-page values,
    not a document-global constant — found on testData/pic_programmer.zip).

    `layer="FRAME"` on `<shape>` is part of the unified symbol/schematic
    layer namespace (ir_schema.md "Слои") with a reserved geometric role —
    documented in ir_schema.md "Frame" alongside this change.
    """
    comp_name = _frame_component_name(width_mm, height_mm)
    if proj_el.find(f'component[@name="{comp_name}"]') is not None:
        return comp_name

    # Symbol-local space, origin = the frame's own CENTER — matching what
    # the instance's x/y already represents (the tile's absolute center,
    # see the canvas-building loop) so the instance transform (pure
    # translation, rot=0/mirror=0) doesn't add a SECOND center offset on
    # top of this one. Previously both the shape AND the instance encoded
    # "half width/height", so the rendered frame landed shifted by a full
    # extra (width/2, -height/2) from where it should be — found by
    # actually composing instance+local coordinates instead of eyeballing
    # the raw stored instance.y. Rect now spans local x:[-w/2,w/2],
    # y:[-h/2,h/2].
    sym_el = ET.Element('symbol', name=comp_name)
    ET.SubElement(sym_el, 'shape',
                  x='0', y='0', w=_um(width_mm), h=_um(height_mm),
                  roundness='0', outline=_um(0.15), rot='0',
                  layer='FRAME')

    # Decorative title-block text — plain custom-attr placeholders (same
    # >ATTRNAME mechanism already used for >VALUE etc.), resolved per
    # INSTANCE (see the canvas-building loop) — Title/Date/Rev/Company are
    # real PER-PAGE values in KiCad (confirmed: testData/pic_programmer.zip
    # has a different date/rev on each page), not a document-global
    # constant, so nothing is baked in here. Stacked bottom-right corner
    # (local (w/2,-h/2) now that origin is the center), a simplified
    # stand-in for KiCad's real worksheet table, not a pixel-faithful
    # reproduction (decorative layer — ir_schema.md "Frame": "любой другой
    # слой... без ограничений").
    margin_mm = 3
    for i, placeholder in enumerate(('>TITLE', '>COMPANY', '>REV', '>DATE')):
        t = ET.SubElement(sym_el, 'text',
                           x=_um(width_mm / 2 - margin_mm),
                           y=_um(-(height_mm / 2 - margin_mm - i * 4)),
                           size=_um(2), rot='0', align='bottom-right', layer='INFO')
        t.text = placeholder

    symbols_el.append(sym_el)
    pool[comp_name] = sym_el

    comp_el = ET.Element('component', name=comp_name, prefix='FRAME', symbol=comp_name, synth='frame')
    proj_el.append(comp_el)
    return comp_name


# ---------------------------------------------------------------------------
# Symbol template -> pool + component info (project-import variant)
# ---------------------------------------------------------------------------

def _convert_schematic_symbol(symbol, pool, symbols_el):
    """Field handling per the user's explicit rules (no per-field hide-
    blanking/special-casing — Value and every custom Field are treated
    exactly the same way):

    1. Every Field on the symbol is collected, no exceptions besides
       Reference (which names/prefixes the device, not an attribute).
    2. Every Field becomes an `<attr>` on the device, **regardless of
       visibility**, with its real value.
    3. The symbol body only gets a >PLACEHOLDER text for a Field that is
       actually visible (`not _is_hidden`) — position/size/justify always
       come from that Field's own real effects.

    `symbol` here is always freshly resolved from the real library (see
    convert_project/_resolve_symbol) — its own Footprint property (if any)
    is just whatever default the library symbol happens to carry, NOT used
    for footprint matching (that comes from the schematic instance, see
    module docstring point 1), so it's treated like any other Field.

    Returns component info (prefix/attrs/gates). Power-flag symbols (#PWR)
    get a component too — no footprint, single `sup` pin — see the
    isPower branch below (mirrors kicad_parser._convert_symbol).

    Pool symbol names are bare `entryName` (+ gate letter), NOT `libId` —
    the caller (convert_project) already scopes pool/symbols_el to one
    output library per source nickname, so there's no cross-library
    collision to guard against, and a library should never carry its own
    name baked into its own symbol/component names.
    """
    if getattr(symbol, 'extends', None):
        # Derived (extends) symbol in PROJECT import = broken precondition,
        # not a case to skip around: the archive is required to carry
        # fully-resolved symbols (KiCad's own "Export Symbols to New
        # Library..." resolves extends away — confirmed on
        # testData/vimdrones.zip, an export-produced library with zero
        # `(extends)` tokens). The library-only path (kicad_parser.convert)
        # keeps its skip — no such precondition there.
        raise ValueError(
            f'{symbol.libId}: derived symbol (extends "{symbol.extends}") in the '
            f'project archive — the archive must carry fully-resolved symbols. '
            f'Re-run KiCad\'s "Export Symbols to New Library...", include that '
            f'library, and re-archive (see module docstring preconditions).')

    ref_prop = next((p for p in symbol.properties if p.key == 'Reference'), None)
    prefix = (ref_prop.value.rstrip('?') if ref_prop and ref_prop.value else '') or 'U'
    names_hidden = symbol.pinNamesHide
    numbers_hidden = symbol.hidePinNumbers

    gate_info = []
    # A symbol with NO units at all (0 pins, 0 graphics — previously
    # mislabeled "extends" and skipped, silently dropping the component AND
    # its footprint from the pool) is a real, legitimate case: a
    # footprint-only part such as a motor mount / mechanical fixture
    # (confirmed real: testData/vimdrones.zip "Conn_01x03_Socket_1",
    # Reference MOTOR_MOUNT1, footprint 1404_4300kv_motor, deliberately
    # stripped of all graphics by the project author). Imported honestly:
    # an EMPTY <symbol>, a component with the footprint, empty pin-mapping
    # — the board part survives for the future PCB import, the schematic
    # shows just the designator/value at the placement point.
    # (_group_gates on a unit-less symbol would yield [(None, [None])] via
    # its common_body slot — the empty case is branched explicitly instead.)
    for letter, bodies in (_group_gates(symbol) if symbol.units else [(None, [])]):
        sym_name = symbol.entryName if letter is None else f'{symbol.entryName}_{letter}'
        sym_el, named_pins = _build_gate_symbol(sym_name, bodies, names_hidden, numbers_hidden)

        for p in symbol.properties:
            if _is_hidden(p) or p.key in _KICAD_RESERVED_PROP_KEYS:
                continue
            if p.key == 'Reference':
                placeholder = 'NAME'
            elif p.key == 'Value':
                placeholder = 'VALUE'
            else:
                # Eagle recognizes ONLY uppercase >PLACEHOLDER text;
                # attr keys keep their own case (matching is case-
                # insensitive everywhere: Eagle itself, svg_renderer,
                # kicad_exporter's attrs_lower)
                placeholder = clean_attr_name(p.key).upper()
            if placeholder:
                _emit_property_placeholder(sym_el, placeholder, p)

        if sym_name not in pool:
            symbols_el.append(sym_el)
            pool[sym_name] = sym_el
        gate_info.append((letter, sym_name, named_pins))

    if symbol.isPower and not _is_erc_power_source(symbol):
        # #PWR power-flag symbol — see kicad_parser._convert_symbol's isPower
        # branch for the full rationale (KLC S7.1 + ir_schema.md "Supply
        # symbol"): collapse KiCad's Value-names-the-net + blank-pin-name
        # split into IR's single `sup`-pin-name source. PWR_FLAG-style
        # power_out symbols are excluded (ERC driver directive, not a rail —
        # see kicad_parser._is_erc_power_source): they import as ordinary
        # components whose pin never names the net.
        value_prop = next((p for p in symbol.properties if p.key == 'Value'), None)
        pin_el = pool[sym_name].find('pin')
        if pin_el is not None and value_prop and value_prop.value:
            pin_el.set('name', clean_attr_name(_kicad_overbar_to_eagle(value_prop.value)))
            pin_el.set('direction', 'sup')
    elif _is_erc_power_source(symbol):
        import_log.log(symbol.libId or symbol.entryName, '',
                        'POWER_SYMBOL with power_out pin (PWR_FLAG-style ERC '
                        'driver directive) — imported as an ordinary symbol, '
                        'its pin does not name the net')

    attrs = []
    for p in symbol.properties:
        if p.key == 'Reference':
            continue
        key = clean_attr_name(p.key).lower()
        if key:
            attrs.append((key, '' if _kicad_blank(p.value) else _kicad_overbar_to_eagle(p.value)))

    return {'prefix': prefix, 'attrs': attrs, 'gates': gate_info}


# ---------------------------------------------------------------------------
# Board loading (embedded 3D models only — see module docstring)
# ---------------------------------------------------------------------------

_BARE_NET_RE = re.compile(r'\(net "')


def _read_board(pcb_path):
    """Board.from_file, tolerating a legacy pad-level `(net "name")` form
    (no net number — kiutils' Net.from_sexpr unconditionally indexes exp[2]
    and crashes with IndexError otherwise).

    Confirmed against testData/video/video.kicad_pcb (an older/converted
    board): every single one of its ~10800 pad `net` tokens omits the
    number, consistently (no mix with the normal `(net N "name")` form), so
    this isn't a one-off typo to special-case — just an older writer's
    format. We don't use net assignments at all, so a placeholder number 0
    is enough to make kiutils accept the file.
    """
    text = Path(pcb_path).read_text(encoding='utf-8')
    patched = _BARE_NET_RE.sub('(net 0 "', text)
    return Board.from_sexpr(_kiutils_sexpr.parse_sexp(patched))


# ---------------------------------------------------------------------------
# Project entry point
# ---------------------------------------------------------------------------

def _find_project_root(src):
    """A .zip archive or an already-extracted project dir -> (project_dir,
    kicad_pro_path). Per module docstring's archive precondition: a
    KiCad-archived zip is expected to contain exactly one *.kicad_pro at its
    root (confirmed against testData/video.zip).

    A zip is extracted to a fresh OS temp dir, not next to the source file —
    this is throwaway working data for one conversion run, not something to
    inspect afterwards (see progress.md: outputs/ is for run artifacts worth
    keeping, testData/ for fixtures — a temp dir is the right place for
    neither).
    """
    src = Path(src)
    if src.is_file() and src.suffix.lower() == '.zip':
        extract_dir = Path(tempfile.mkdtemp(prefix='kicad_project_'))
        with zipfile.ZipFile(src) as zf:
            zf.extractall(extract_dir)
        src = extract_dir
    if src.is_file() and src.suffix == '.kicad_pro':
        return src.parent, src
    pros = list(src.glob('*.kicad_pro'))
    if len(pros) != 1:
        raise ValueError(f'expected exactly one .kicad_pro in {src}, found {len(pros)}')
    return src, pros[0]


def _nickname_and_entry(lib_id, fallback_nickname):
    """'NICKNAME:ENTRY' -> (NICKNAME, ENTRY). A symbol with no registered
    nickname (Symbol.libId falls back to bare entryName — happens for
    power-flag symbols and the like) buckets under fallback_nickname instead
    of losing its own name to look like one.
    """
    if ':' in lib_id:
        nickname, _, entry = lib_id.partition(':')
        return nickname, entry
    return fallback_nickname, lib_id


def convert_project(src, output_dir):
    """Returns the list of written .swlib paths — one per SOURCE symbol
    library nickname (sym-lib-table), not one big merged library.

    A real project commonly pulls symbols from several distinct libraries
    (confirmed: testData/video references at least two) — dumping them all
    into one output library would force `NICKNAME:ENTRY`-style names
    everywhere just to avoid cross-library collisions. Splitting by nickname
    instead keeps every symbol/component name bare, exactly like the actual
    source libraries were organized, and the nickname only ever appears once,
    as the output library's own name/filename.
    """
    project_dir, pro_path = _find_project_root(src)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pcb_path = project_dir / f'{pro_path.stem}.kicad_pcb'
    top_sch_path = project_dir / f'{pro_path.stem}.kicad_sch'

    # Shared one-time extraction — which output library ends up using a given
    # footprint's model doesn't matter for decoding it, only for where the
    # final sidecar copy lands (see out_models_dir below, per nickname).
    embedded_dir = output_dir / '_embedded_raw'
    embedded_names = _extract_embedded_models(pcb_path, embedded_dir)

    sym_lib_table = None
    sym_lib_table_path = project_dir / 'sym-lib-table'
    if sym_lib_table_path.exists():
        sym_lib_table = LibTable.from_file(str(sym_lib_table_path), encoding='utf-8')
    fp_lib_table = None
    fp_lib_table_path = project_dir / 'fp-lib-table'
    if fp_lib_table_path.exists():
        fp_lib_table = LibTable.from_file(str(fp_lib_table_path), encoding='utf-8')
    sym_lib_table, fp_lib_table = _augment_lib_tables(project_dir, sym_lib_table, fp_lib_table)

    schematics = _load_schematic_tree(top_sch_path)

    # Per-instance Footprint property is the ONLY source for symbol<->
    # footprint pairing now (see module docstring point 1) — no board
    # path/UUID matching. lib_id -> set of distinct footprint refs used,
    # for generic-merge.
    footprint_refs_by_libid = {}
    for sch in schematics.values():
        for sym in sch.schematicSymbols:
            # Power-flag symbols (#PWR — Reference starts with "#") flow
            # through unchanged: their Footprint property is always blank
            # (see _convert_schematic_symbol's isPower branch), so they
            # naturally end up with an empty fp_refs set below, same as any
            # other footprint-less symbol — no special-casing needed here.
            lib_id = f'{sym.libraryNickname}:{sym.entryName}' if sym.libraryNickname else sym.entryName
            fp_prop = next((p for p in sym.properties if p.key == 'Footprint'), None)
            fp_ref = fp_prop.value if fp_prop and not _kicad_blank(fp_prop.value) else None
            bucket = footprint_refs_by_libid.setdefault(lib_id, set())
            if fp_ref:
                bucket.add(fp_ref)

    # Resolve every used symbol fresh from sym-lib-table — never trust the
    # schematic's own cached lib_symbols (see module docstring precondition 3).
    sym_lib_cache = {}
    template_by_libid = {}
    for lib_id in footprint_refs_by_libid:
        sym = _resolve_symbol(lib_id, project_dir, sym_lib_table, sym_lib_cache)
        if sym is None:
            # Hard fail, not skip-with-warning: per the user, the project
            # archive is required to carry every library it uses — if a
            # symbol isn't found among the libraries actually registered in
            # sym-lib-table, that's the user's to fix (export the symbol /
            # add the missing library to the archive), not ours to silently
            # work around or guess at.
            raise ValueError(
                f'{lib_id}: symbol not found in any library registered in '
                f'sym-lib-table. Export it (KiCad: "Export Symbols to New '
                f'Library...") and include that library in the project '
                f'archive before converting.')
        template_by_libid[lib_id] = sym

    # Resolve every distinct footprint reference fresh from fp-lib-table —
    # never trust the board's own cached/possibly-hand-edited copy (same
    # precondition).
    fp_cache = {}
    groups = {}   # lib_id -> {footprint_ref: Footprint}
    for lib_id, fp_refs in footprint_refs_by_libid.items():
        for fp_ref in fp_refs:
            fp_path = _resolve_footprint_path(fp_ref, project_dir, fp_lib_table)
            if fp_path is None:
                # Same hard-fail principle as the symbol loop above — see its
                # comment.
                raise ValueError(
                    f'{fp_ref}: footprint not found in any library registered '
                    f'in fp-lib-table. Export it (KiCad: "Export Footprints to '
                    f'New Library...") and include that library in the '
                    f'project archive before converting.')
            if fp_path not in fp_cache:
                fp_cache[fp_path] = _read_footprint(fp_path)
            groups.setdefault(lib_id, {})[fp_ref] = fp_cache[fp_path]

    by_nickname = {}
    for lib_id, sym_template in template_by_libid.items():
        nickname, entry = _nickname_and_entry(lib_id, pro_path.stem)
        by_nickname.setdefault(nickname, {})[entry] = (lib_id, sym_template)

    written = []
    for nickname, entries in by_nickname.items():
        lib_el = ET.Element('library', name=nickname)
        symbols_el = ET.SubElement(lib_el, 'symbols')
        pool = {}
        n_components = 0
        n_generic = 0

        # Sidecar dir bare-named after THIS library (ir_schema.md canon),
        # same rule as kicad_parser.py's single-file case, just per-nickname
        # now instead of per-whole-project.
        out_models_dir = output_dir / sanitize_filename(nickname)

        for entry, (lib_id, sym_template) in entries.items():
            info = _convert_schematic_symbol(sym_template, pool, symbols_el)
            if info is None:
                continue   # derived (extends) symbol — not yet supported

            fp_variants = groups.get(lib_id, {})
            if len(fp_variants) > 1:
                n_generic += 1

            comp_el = ET.Element('component', name=entry, prefix=info['prefix'])
            gates = info['gates']
            if len(gates) == 1 and gates[0][0] is None:
                comp_el.set('symbol', gates[0][1])
            else:
                for letter, sym_name, _ in gates:
                    ET.SubElement(comp_el, 'gate', name=letter, symbol=sym_name)

            for fp in fp_variants.values():
                fp_name = fp.entryName
                fp_el, pad_name_groups = _convert_footprint(fp, fp_name)
                _copy_model3d(fp, fp_el, embedded_dir if embedded_names else None, out_models_dir, fp_name)

                comp_fp = ET.SubElement(comp_el, 'footprint', name=fp_name)
                for child in fp_el:
                    comp_fp.append(child)
                pm_el = ET.SubElement(comp_fp, 'pin-mapping')
                if len(gates) == 1 and gates[0][0] is None:
                    _, _, named_pins = gates[0]
                    for ir_name, pad_num in _pin_mapping(named_pins, pad_name_groups):
                        ET.SubElement(pm_el, 'map', pin=ir_name, pad=pad_num)
                else:
                    for letter, _, named_pins in gates:
                        for ir_name, pad_num in _pin_mapping(named_pins, pad_name_groups):
                            ET.SubElement(pm_el, 'map', pin=f'{letter}.{ir_name}', pad=pad_num)

            if info['attrs']:
                attrs_el = ET.SubElement(comp_el, 'attributes')
                for k, v in info['attrs']:
                    ET.SubElement(attrs_el, 'attr', name=k, value=v)

            lib_el.append(comp_el)
            n_components += 1

        lib_out_path = output_dir / f'{sanitize_filename(nickname)}.swlib'
        raw = minidom.parseString(ET.tostring(lib_el, encoding='unicode')).toprettyxml(indent='  ')
        lib_out_path.write_text(raw, encoding='utf-8')
        print(f'Written: {lib_out_path}  ({n_components} components, {n_generic} multi-footprint)')
        import_log.write(lib_out_path)
        written.append(str(lib_out_path))

    return written


def _augment_lib_tables(project_dir, sym_lib_table, fp_lib_table):
    """Register root-level libraries the tables missed (per the user,
    revising decisions.md «Резолв — без эвристик»: a *.kicad_sym / *.pretty
    physically sitting at the archive ROOT is part of the handed-over
    project by construction — registering it is still reading what's
    already in the archive, not hunting the filesystem; confirmed real,
    testData/multichannel.zip carries multichannel.kicad_sym +
    Library.pretty next to an EMPTY sym-lib-table and no fp-lib-table at
    all). Existing table entries keep priority: additions are APPENDED, so
    the resolvers' registered-nickname-first order is unchanged; dedup is
    by resolved path. Each auto-registration is logged, not silent."""
    from kiutils.libraries import Library

    def _registered_paths(table):
        if table is None:
            return set()
        return {Path(lib.uri.replace('${KIPRJMOD}', str(project_dir))).resolve()
                for lib in table.libs}

    sym_lib_table = sym_lib_table if sym_lib_table is not None else LibTable(type='sym_lib_table')
    seen = _registered_paths(sym_lib_table)
    for p in sorted(project_dir.glob('*.kicad_sym')):
        if p.resolve() in seen:
            continue
        sym_lib_table.libs.append(Library(name=p.stem, uri=str(p)))
        import_log.log('sym-lib-table', p.name, 'LIB_AUTOREGISTERED from archive root')

    fp_lib_table = fp_lib_table if fp_lib_table is not None else LibTable(type='fp_lib_table')
    seen = _registered_paths(fp_lib_table)
    for p in sorted(project_dir.glob('*.pretty')):
        if not p.is_dir() or p.resolve() in seen:
            continue
        fp_lib_table.libs.append(Library(name=p.stem, uri=str(p)))
        import_log.log('fp-lib-table', p.name, 'LIB_AUTOREGISTERED from archive root')

    return sym_lib_table, fp_lib_table


# ---------------------------------------------------------------------------
# Hierarchy: (sheet) -> IR <module> (ir_schema.md "Модуль (design block + иерархия)")
# ---------------------------------------------------------------------------

# KiCad sheet-pin `connectionType` -> IR <port> `direction`. Same source
# enum as global/hierarchical label `shape` (KiCad reuses it), but the
# TARGET differs: label shape -> presentation-only <label style=...>
# (_LABEL_STYLE above), sheet pin -> the module port's ELECTRICAL direction
# (ir_schema.md "Модуль": direction is an explicit port attribute, never
# derived from the inner net's pins). `tri_state` has no port direction of
# its own in IR -> `io` (electrically closest), the same collapse
# _LABEL_STYLE applies to the label rendering of that source value.
_PORT_DIRECTION = {
    'input': 'in', 'output': 'out', 'bidirectional': 'io',
    'tri_state': 'io', 'passive': 'pas',
}


# Parent-canvas wire stub drawn outward from every SYNTHESIZED port (see
# convert_project_full's module loop): long enough that the Eagle
# exporter's port-pin trim (eagle_exporter._PORT_PIN_LEN_UM, 5.08mm) still
# leaves a visible 2.54mm tail carrying the label past the pin's far end.
_SYNTH_PORT_TAIL_MM = 7.62


def _synth_port_side(occupied):
    """The ONE side that receives ALL of a module's synthesized ports: the
    side with the fewest REAL ports (tie order: left, right, top, bottom —
    per the user). One side for all of them, never spilling onto another:
    the first version spread them across least-crowded sides individually,
    and two neighbouring instances immediately shorted their stubs into
    each other across the gap between facing edges (bottom-of-CH1's GND
    stub landed exactly on top-of-CH2's +1V1 stub, caught by _build_nets'
    conflicting-label check) — opposite-side stubs point at each other by
    construction, same-side stubs all point the same way and can't meet."""
    sides = ('left', 'right', 'top', 'bottom')
    return min(sides, key=lambda s: (len(occupied[s]), sides.index(s)))


def _alloc_port_slot(occupied_coords):
    """Next free 2.54mm slot along one side, walking outward from the
    center (0, -2.54, +2.54, -5.08, ...), skipping anything within 1.27mm
    of a taken coord. Deliberately UNBOUNDED — on a block too small for its
    port count the row simply continues past the corners (ugly but
    deterministic and electrically clean), rather than spilling onto
    another side (see _synth_port_side for why that was worse). Mutates
    `occupied_coords`."""
    k = 0
    while True:
        for c in ((0,) if k == 0 else (-k * 2540, k * 2540)):
            if all(abs(c - o) >= 1270 for o in occupied_coords):
                occupied_coords.append(c)
                return c
        k += 1


def _collect_modules(pages, project_dir):
    """Every (sheet) symbol across all top-level pages -> ({stem: (path,
    Schematic)}, [(page_idx, HierarchicalSheet, stem), ...]).

    Deduplicated by FILE: a sheet instantiated N times (ground truth
    testData/multichannel — channel_strip.kicad_sch x4) is loaded once and
    becomes ONE <module> definition; each (sheet) occurrence is one
    placement of it (an <instance module=...> on the parent canvas).

    Depth is checked strictly (ir_schema.md "Общая модель": вложенность
    запрещена, глубина ровно 1; deeper import is a hard reject, NEVER
    flattening — silently merging different local nets by name collision is
    exactly the failure mode that rule exists to prevent): a module file
    that itself contains (sheet) symbols aborts the whole conversion,
    naming the chain of files.
    """
    modules = {}
    sheet_uses = []
    for page_idx, (page_path, sch) in enumerate(pages):
        for sheet in sch.sheets:
            fname = sheet.fileName.value
            mod_path = project_dir / fname
            stem = mod_path.stem
            if stem not in modules:
                mod_sch = Schematic.from_file(str(mod_path), encoding='utf-8')
                if mod_sch.sheets:
                    nested = ', '.join(s.fileName.value for s in mod_sch.sheets)
                    raise ValueError(
                        f'{page_path.name} -> {fname}: module file contains (sheet) '
                        f'symbol(s) of its own ({nested}) — hierarchy deeper than one '
                        f'level is not supported and is never flattened (ir_schema.md '
                        f'"Модуль": вложенность запрещена, глубина ровно 1). '
                        f'Restructure the design to a single sheet level and re-export.')
                if mod_sch.busEntries:
                    raise ValueError(f'{fname}: bus entries found in the module '
                                      f'schematic — not yet supported.')
                modules[stem] = (mod_path, mod_sch)
            sheet_uses.append((page_idx, sheet, stem))
    return modules, sheet_uses


def _sheet_ports(sheet):
    """One (sheet) occurrence's pins -> [(name, direction, side, coord_um_str)]
    in IR port terms (ir_schema.md "Модуль": `side` = which of the 4 edges,
    `coord` = signed offset along that edge FROM THE BLOCK CENTER).

    KiCad's sheet `position` is the TOP-LEFT corner in its Y-down space
    (confirmed on testData/multichannel: sheet at (74.93, 55.88), size
    40.64x5.08, pin "OUT" at (115.57, 58.42) = right edge, vertical
    center). Side is classified by NEAREST edge rather than exact equality
    — guards against source rounding; coord flips sign on left/right sides
    (KiCad +y is down, IR coord along a vertical edge is +up).
    """
    cx = sheet.position.X + sheet.width / 2
    cy = sheet.position.Y + sheet.height / 2
    out = []
    for pin in sheet.pins:
        rx = pin.position.X - cx
        ry = pin.position.Y - cy
        # Distances rounded to whole µm before comparing: a pin sitting
        # EXACTLY on a corner (real case: a synthesized port slot ±2.54mm on
        # a 5.08mm-tall block lands on both edges at once) is equidistant
        # from two sides, and raw float noise broke the tie differently for
        # different instances of the SAME sheet file — µm-int distances +
        # the fixed left/right/top/bottom order make the choice
        # deterministic (same tie order as _synth_port_side).
        dist = {
            'left': round(abs(rx + sheet.width / 2) * 1000),
            'right': round(abs(rx - sheet.width / 2) * 1000),
            'top': round(abs(ry + sheet.height / 2) * 1000),
            'bottom': round(abs(ry - sheet.height / 2) * 1000),
        }
        side = min(dist, key=dist.get)
        coord_mm = rx if side in ('top', 'bottom') else -ry
        out.append((pin.name, _PORT_DIRECTION.get(pin.connectionType, 'io'),
                    side, _um(coord_mm)))
    return out


def _describe_sheet_divergence(first_ports, first, other):
    """A short human-readable reason two occurrences of the same module file
    differ, for the hard-reject message (see _collect_modules)."""
    if (other.width, other.height) != (first.width, first.height):
        return (f'block size {other.width:g}x{other.height:g}mm vs '
                f'{first.width:g}x{first.height:g}mm')
    first_by_name = {p[0]: p for p in first_ports}
    other_by_name = {p[0]: p for p in _sheet_ports(other)}
    missing = sorted(set(first_by_name) - set(other_by_name))
    extra = sorted(set(other_by_name) - set(first_by_name))
    if missing:
        return f'port {missing[0]!r} present on the first instance but missing here'
    if extra:
        return f'port {extra[0]!r} present here but not on the first instance'
    for name, fp in first_by_name.items():
        op = other_by_name[name]
        if fp[2:] != op[2:]:
            return (f'port {name!r} at {op[2]}/{op[3]}um here vs '
                    f'{fp[2]}/{fp[3]}um on the first instance')
    return 'sheet pin layout differs'


class _Canvas:
    """Accumulated content of ONE IR canvas — either the top-level
    <schematic> (shared across all tiled pages, so same-named cross-page
    global labels unite into one net, as before) or one <module>'s own
    canvas with its own separate net space (ir_schema.md "Модуль": port
    names are the only link between module and parent)."""

    def __init__(self):
        self.instances = []
        self.pin_points = []
        self.wires = []
        self.wire_width = {}
        self.junction_points = set()
        self.label_records = []
        self.deco_lines = []    # (x1,y1,x2,y2,width_um) — GRAPHIC layer, canvas-direct
        self.deco_shapes = []   # (kind,cx,cy,w,h,roundness,outline_um) — GRAPHIC layer
        self.deco_arcs = []     # (cx,cy,r_um,start_deg,sweep_deg,width_um) — GRAPHIC layer
        self.deco_texts = []    # (x_um,y_um raw,text,size_um_str,rot_deg,align) — GRAPHIC layer
        self.deco_notes = []    # (x_um,y_um raw top-left,w_um,markdown,rot_deg) — <note>
        self.portref_keys = set()   # (inst_name, port_name) -> written as <portref>, not <pinref>
        self.global_sup_names = set()   # net names carried by GLOBAL supply pins
        self.local_sup_names = set()    # ...by (power local) supply pins (KiCad 10)
        self.n_skipped_multi_unit = 0
        self.n_skipped_multi_gate = 0


def _instance_hidden_prop_keys(text):
    """{(placement uuid, property key)} for every placed schematic symbol
    property carrying `(hide yes)` — the same kiutils 1.4.8 read gap
    _hidden_keys_from_symbol_nodes works around for LIBRARY symbols, at the
    placement level (kiutils reads hide=False for every one of them)."""
    tree = _kiutils_sexpr.parse_sexp(text)
    out = set()
    for item in tree[1:]:
        if not (isinstance(item, list) and item and item[0] == 'symbol'):
            continue
        u = next((sub[1] for sub in item
                  if isinstance(sub, list) and sub and sub[0] == 'uuid'), None)
        for sub in item:
            if isinstance(sub, list) and sub and sub[0] == 'property' \
                    and _contains_hide_yes(sub):
                out.add((u, sub[1]))
    return out


def _field_overrides(sym, sym_el, inst_x_um, inst_y_um, ir_rot, ir_mirror, dx,
                     hidden_props):
    """KiCad per-instance field placements -> IR <instance><text> overrides
    (Eagle's smashed records) — the exact inverse of the exporter's
    _abs_style: a field is an override only when it DIFFERS from the
    library placeholder's default (position under the field transform —
    negate-theta when mirrored — or visibility); everything inherited
    emits nothing (one fact, one place). A hidden property whose library
    placeholder is visible becomes a suppression-only record (hidden=yes,
    no geometry). Without this, every smashed Eagle schematic came back
    un-smashed with fields at library defaults (closed-loop catch: the
    user hand-restored IC2's records to show the difference)."""
    styles = {}
    for t in sym_el.findall('text'):
        s = (t.text or '').strip()
        if s.startswith('>'):
            styles[s[1:].lower()] = (
                float(t.get('x', 0)), float(t.get('y', 0)),
                float(t.get('rot', 0) or 0),
                int(t.get('size', 1778)), t.get('align', 'bottom-left'))
    out = []
    for p in sym.properties:
        if p.key in ('Footprint', 'Datasheet') \
                or p.key in _KICAD_RESERVED_PROP_KEYS:
            continue
        key = ('NAME' if p.key == 'Reference'
               else 'VALUE' if p.key == 'Value'
               else clean_attr_name(p.key).upper())
        if not key:
            continue
        lib = styles.get(key.lower())
        if _is_hidden(p) or (sym.uuid, p.key) in hidden_props:
            if lib is not None:
                out.append({'_text': f'>{key}', 'hidden': 'yes'})
            continue
        ax = (p.position.X + dx) * 1000
        ay = -p.position.Y * 1000
        aang = float(p.position.angle or 0) % 360
        asize = int(_text_size_um(p.effects))
        aalign = _align(p.effects.justify if p.effects else None)
        if lib is not None:
            lx, ly, lrot, lsize, lalign = lib
            ex, ey, erot = field_to_kicad(inst_x_um, inst_y_um,
                                          ir_rot, ir_mirror, lx, ly, lrot)
            if (abs(ex - ax) < 5 and abs(ey - ay) < 5
                    and (aang - erot) % 180 == 0
                    and abs(asize - lsize) <= 1 and aalign == lalign):
                continue
        lx, ly, lrot = field_from_kicad(inst_x_um, inst_y_um,
                                        ir_rot, ir_mirror, ax, ay, aang)
        rec = {'_text': f'>{key}', 'x': str(round(lx)), 'y': str(round(ly)),
               'size': str(asize), 'align': aalign, 'font': 'vector'}
        if lrot:
            rec['rot'] = _f(lrot)
        out.append(rec)
    return out


def _collect_canvas(sch, sch_path, dx, cv, ctx, stray_check):
    """One KiCad schematic file's canvas content (placed symbols, wires,
    junctions, labels, decorative geometry) accumulated into `cv`.

    Shared verbatim between a top-level page (stray_check = that page's
    frame-bbox closure, dx = its tiling shift) and a module file's canvas
    (stray_check=None — a module is a design block, not a printable page:
    no page frame, hence nothing to validate strays against; dx=0). The
    body IS the former inline page-loop of convert_project_full, factored
    out unchanged when (sheet)->module import was added — every comment
    below predates that split and still applies to both callers.

    `ctx` carries the pool/lookup tables built by convert_project_full's
    resolution phase (read-only here).
    """
    if stray_check is None:
        def stray_check(label, x0, x1, y0, y1):
            pass
    pool = ctx['pool']
    gates_by_libid = ctx['gates_by_libid']
    gate_letter_by_libid = ctx['gate_letter_by_libid']
    comp_name_by_libid = ctx['comp_name_by_libid']
    library_by_libid = ctx['library_by_libid']
    groups = ctx['groups']
    # kiutils (hide yes) read gap, placement-level (see the function)
    hidden_props = _instance_hidden_prop_keys(
        Path(sch_path).read_text(encoding='utf-8'))

    # Supply locality is a per-FILE fact (lib_symbols cache copies) — see
    # _power_locality.
    locality = _power_locality(sch_path)

    for sym in sch.schematicSymbols:
        lib_id = f'{sym.libraryNickname}:{sym.entryName}' if sym.libraryNickname else sym.entryName
        if ctx['power_value_virt']:
            _v = next((p.value for p in sym.properties if p.key == 'Value'), None)
            lib_id = ctx['power_value_virt'].get((lib_id, _v), lib_id)
        gates = gates_by_libid[lib_id]

        # Single-mode component (no <gate>, gates == [(None, sym_name,
        # named_pins)]) — sym.unit must be the trivial 1/None case,
        # anything else would mean the library symbol has real gates
        # that _group_gates already grouped away (shouldn't happen —
        # single-mode is defined by <=1 real unit besides unit 0).
        if len(gates) == 1 and gates[0][0] is None:
            if sym.unit not in (None, 1):
                cv.n_skipped_multi_unit += 1
                continue
            gate_letter = None
            _, sym_name, named_pins = gates[0]
        else:
            # Multi-gate component — ir_schema.md "Размещение
            # многорежимного компонента": each KiCad unit placement is
            # its OWN <instance>, same designator, `gate` attribute
            # says which one. sym.unit maps to a letter via the SAME
            # sorted-real-unit-ids assignment _group_gates used when
            # building the pool (_unit_id_to_gate_letter mirrors that
            # logic independently, see its docstring for why it's not
            # threaded out of _group_gates directly).
            letter_by_unit = gate_letter_by_libid[lib_id]
            gate_letter = letter_by_unit.get(sym.unit)
            if gate_letter is None:
                # sym.unit is 0/None/unrecognized on a symbol that DOES
                # have real gates — KiCad shouldn't produce this (every
                # placed instance of a multi-gate symbol has unit>=1),
                # but if it ever does there's no gate to attribute this
                # placement to.
                cv.n_skipped_multi_gate += 1
                continue
            gate_entry = next((g for g in gates if g[0] == gate_letter), None)
            if gate_entry is None:
                cv.n_skipped_multi_gate += 1
                continue
            _, sym_name, named_pins = gate_entry

        ref_prop = next((p for p in sym.properties if p.key == 'Reference'), None)
        designator = ref_prop.value if ref_prop and ref_prop.value else sym.uuid

        # Per-instance Value override (e.g. "10k" on a generic Device:R
        # placed as R1, "4.7k" on the next one) — deliberately NOT
        # folded into the pool component's shared <attributes> (see
        # _convert_schematic_symbol docstring: that's device-level, one
        # value for every instance, wrong for generics). ir_schema.md
        # "Component instance": per-instance attr override, <attr>
        # child of <instance>.
        #
        # Multi-gate: Value lives on the KiCad Property of EVERY placed
        # unit (same real-world chip, same string on each) — written
        # only on the FIRST gate's <instance> (ir_schema.md "Размещение
        # многорежимного компонента": one physical chip, one Value, not
        # one per gate) — every gate letter besides 'A' skips this.
        inst_attrs = []
        if gate_letter in (None, 'A'):
            comp_defaults = ctx['comp_attrs_by_libid'].get(lib_id, {})
            value_prop = next((p for p in sym.properties if p.key == 'Value'), None)
            if value_prop and not _kicad_blank(value_prop.value):
                v = _kicad_overbar_to_eagle(value_prop.value)
                # An instance Value equal to the entry name or the library
                # default is the fallback both KiCad and Eagle display for
                # "no value set" (our own exporter writes `value or
                # comp_name`) — materializing it would stamp the component
                # name into every instance's value on the way back to Eagle.
                entry = lib_id.split(':', 1)[-1]
                if v not in (entry, comp_defaults.get('value')):
                    inst_attrs.append(('value', v))
            # Per-instance power Value rename (KiCad allows GND -> AGND on
            # one placed symbol) is not expressible in IR — the net name
            # lives on the library symbol's sup PIN, one per symbol. Reject
            # with a pointer, never guess (feedback: легко и однозначно).
            sup_pins = [p.get('name')
                        for p in ctx['pool'][sym_name].findall('pin')
                        if p.get('direction') == 'sup']
            if sup_pins and value_prop and value_prop.value:
                v = clean_attr_name(_kicad_overbar_to_eagle(value_prop.value))
                if v not in sup_pins:
                    raise ValueError(
                        f'{sch_path.name}: power symbol {designator} '
                        f'({lib_id}) has per-instance Value {v!r} != library '
                        f'net name {sup_pins[0]!r} — per-instance power '
                        f'renames are not supported; make a dedicated power '
                        f'symbol for that net in the library and re-export.')
            # Every OTHER instance property is a device attribute (same
            # collect-ALL rule the library side already follows —
            # decisions.md "Field-парсинг"; found lost by the closed-loop
            # oracle: tolmach's manf/digikey#/ru etc. survived to the
            # .kicad_sch but never came back to IR). Dedup against the
            # pool component's own <attributes> — a value equal to the
            # component default is inherited, not an override.
            for p in sym.properties:
                if p.key in ('Reference', 'Value', 'Footprint'):
                    continue
                if _kicad_blank(p.value):
                    continue
                key = 'datasheet' if p.key == 'Datasheet' else p.key
                val = _kicad_overbar_to_eagle(p.value)
                # pool attrs store keys lowercased (kicad_parser rule)
                if val in (comp_defaults.get(key), comp_defaults.get(key.lower())):
                    continue
                inst_attrs.append((key, val))

        # Which of the component's (possibly several) <footprint>s THIS
        # instance actually uses — per-instance `Footprint` property,
        # same field already read above to build `groups`/the pool's
        # <footprint> set, just not threaded through to the placed
        # instance until now. Only worth recording when the component
        # has 2+ footprints (ir_schema.md "Component instance") — with
        # exactly one, there's no ambiguity to resolve, so nothing is
        # emitted (matches the existing single-footprint-omits-the-
        # qualifier convention used everywhere else in this project).
        # Found missing while exporting to Eagle: real Eagle rejected
        # `<part device="">` for a multi-footprint deviceset — the
        # fallback (guess the first footprint) silently assigned a
        # WRONG one (e.g. a generic "R" resistor instance that's
        # actually a fuse holder, testData/vimdrones.zip "R6"/"R11").
        inst_footprint = None
        if len(groups.get(lib_id, {})) > 1:
            fp_prop = next((p for p in sym.properties if p.key == 'Footprint'), None)
            fp_ref = fp_prop.value if fp_prop and not _kicad_blank(fp_prop.value) else None
            fp_obj = groups.get(lib_id, {}).get(fp_ref)
            if fp_obj is not None:
                inst_footprint = fp_obj.entryName

        angle = sym.position.angle or 0
        eff_angle, flip_x, ir_rot, ir_mirror = _mirror_and_angle(angle, sym.mirror)
        sym_x = sym.position.X + dx

        # Zip named_pins with the pool's <pin> elements POSITIONALLY,
        # not by name: _convert_schematic_symbol's isPower branch
        # renames the sole pin in-place (e.g. "1" -> "GND") after
        # named_pins was computed, so the IR name actually declared on
        # the symbol can differ from the stale ir_name here — a
        # name-based lookup silently fails for every power-flag symbol
        # (found on testData/phil: GND/3V3 etc. nets came out
        # unnamed). Position is safe because _add_unit_geometry walks
        # named_pins in the same order to emit these very elements.
        sym_el = pool[sym_name]

        # Stray check: full bbox of the PLACED symbol (every line/arc/
        # shape/pin/polygon/text corner, transformed by this instance's
        # own rotation/mirror/position — _bounds gives the symbol-LOCAL
        # extent in its own Y-up library mm convention, same one
        # _abs_pin_pos_mm already expects for a single point) — not
        # just the instance's own placement (x,y) point. A symbol whose
        # origin sits inside the frame but whose body/pins stick out
        # past the edge is still stray (decisions.md "Stray-валидация").
        # _abs_pin_pos_mm now returns IR-space (Y-up) coordinates (see
        # its own docstring) — negate Y once here to get back to the
        # raw KiCad Y-down space stray_check's frame_y0/y1 are in
        # (same convention every other stray_check call in this loop
        # uses), since _abs_pin_pos_mm itself no longer does that flip.
        ir_sym_y = -sym.position.Y
        lx0, lx1, ly0, ly1 = _bounds(sym_el)
        corners = [_abs_pin_pos_mm(cx, cy, sym_x, ir_sym_y, eff_angle, flip_x)
                   for cx in (lx0, lx1) for cy in (ly0, ly1)]
        cxs, cys_ir = [c[0] for c in corners], [c[1] for c in corners]
        cys = [-y for y in cys_ir]
        stray_check(designator, round(min(cxs) * 1000), round(max(cxs) * 1000),
                     round(min(cys) * 1000), round(max(cys) * 1000))

        sym_is_local_supply = _is_local_supply(sym, locality)

        for (ir_name, pin), pin_el in zip(named_pins, sym_el.findall('pin')):
            # Already IR-space (Y-up) — write straight through, no
            # further sign flip (see _abs_pin_pos_mm docstring).
            abs_x, abs_y = _abs_pin_pos_mm(pin.position.X, pin.position.Y,
                                            sym_x, ir_sym_y,
                                            eff_angle, flip_x)
            # Multi-gate: pin name gets the same GATE.pin prefix used in
            # <pin-mapping> (ir_schema.md "<pin-mapping>") — pinref/net
            # naming needs to disambiguate which gate's pin this is,
            # same as the library-side mapping already does.
            raw_name = pin_el.get('name')
            final_name = f'{gate_letter}.{raw_name}' if gate_letter else raw_name
            is_sup = pin_el.get('direction') == 'sup'
            if is_sup:
                # Local vs global decides only whether the net LEAKS across
                # the sheet boundary (module port synthesis); naming inside
                # the canvas is identical either way.
                (cv.local_sup_names if sym_is_local_supply
                 else cv.global_sup_names).add(final_name)
            cv.pin_points.append((_pt(abs_x, -abs_y), designator, final_name, is_sup))

        inst_kwargs = {
            'designator': designator,
            'component': comp_name_by_libid[lib_id],
            'library': library_by_libid[lib_id],
            'footprint': inst_footprint,
            'x': _um(sym_x), 'y': _um(-sym.position.Y),
            'rot': _f(ir_rot), 'mirror': str(ir_mirror),
            'attrs': inst_attrs,
            'texts': _field_overrides(sym, sym_el, sym_x * 1000,
                                      -sym.position.Y * 1000,
                                      ir_rot, ir_mirror, dx, hidden_props),
        }
        if gate_letter:
            inst_kwargs['gate'] = gate_letter
        # Base-configuration assembly flag (ir_schema.md "Варианты сборки"
        # — NOT the variant machinery, the default build's own fact):
        # KiCad dnp -> populate="no". A part that's actually soldered
        # belongs in the BOM by definition — IR has no separate `bom` axis
        # (decided: dnp already implies "not in BOM", and there's no real
        # case for populate=yes + not-in-BOM inside EDA's own scope; that's
        # a procurement-department decision, not a board fact). KiCad lets
        # in_bom=no be set independently of dnp — that combination is
        # logged and ignored, not modeled. on_board=no is REJECTED by
        # decision (a footprint-bearing part that skips the board — in IR
        # "not on the board" is structural, no footprint) — logged, not
        # silently dropped.
        if getattr(sym, 'dnp', None):
            inst_kwargs['populate'] = 'no'
        if sym.inBom is False and not getattr(sym, 'dnp', None):
            import_log.log(sch_path.name, designator,
                            'in_bom=no without dnp ignored (no bom axis in IR, '
                            'see ir_schema.md "Варианты сборки")')
        if sym.onBoard is False:
            import_log.log(sch_path.name, designator,
                            'ON_BOARD=no dropped (rejected flag, see ir_schema.md '
                            '"Варианты сборки")')
        cv.instances.append(inst_kwargs)

    for w in sch.graphicalItems:
        # kiutils lumps THREE different KiCad tokens into one list:
        # `(wire)` (real electrical connection), `(bus)` (a bus trunk —
        # not a single net, not modeled, see sch.busEntries hard-fail
        # above), and `(polyline)` (purely decorative "Graphic Lines"
        # tool output, no electrical meaning at all). Only `type ==
        # 'wire'` Connection objects belong in connectivity — anything
        # else silently became a fake "wire" before this check existed,
        # confirmed by the resulting nets only being catchable once
        # exported (real Eagle rejected the file: coordinates use more
        # digits than `%Coord` allows).
        if getattr(w, 'type', None) != 'wire':
            if getattr(w, 'type', None) == 'bus':
                # Bus trunk — not modeled (see sch.busEntries hard-fail
                # above, ir_schema.md "Отклонено: <bus>"), dropped with a
                # log line, not silently.
                import_log.log(sch_path.name, getattr(w, 'uuid', '') or '(no uuid)',
                                'SCHEMATIC_GRAPHIC bus trunk dropped, not modeled')
                continue
            # `polyline` — decorative "Graphic Lines" tool output (e.g.
            # a logo/drawing, confirmed real: testData/vimdrones.zip's
            # root page has 83 of these). Now has an IR home — direct
            # <line> children of <schematic>, GRAPHIC layer (ir_schema.md
            # "Декоративная геометрия схемы") — no electrical meaning,
            # not fed into wires/_build_nets.
            pts = [_pt(p.X + dx, p.Y) for p in w.points]
            width_um = int(_stroke_width_um(w, ctx['graphic_default_um']))
            for p1, p2 in zip(pts, pts[1:]):
                stray_check(f'polyline {getattr(w, "uuid", "")}',
                             min(p1[0], p2[0]), max(p1[0], p2[0]),
                             min(p1[1], p2[1]), max(p1[1], p2[1]))
                cv.deco_lines.append((p1[0], p1[1], p2[0], p2[1], width_um))
            continue
        pts = [_pt(p.X + dx, p.Y) for p in w.points]
        for p1, p2 in zip(pts, pts[1:]):
            # Stray check: BOTH endpoints — a wire starting inside the
            # frame and ending outside it is still stray (decisions.md
            # "Stray-валидация"), not just its first/center point.
            stray_check(f'wire {getattr(w, "uuid", "")}',
                         min(p1[0], p2[0]), max(p1[0], p2[0]),
                         min(p1[1], p2[1]), max(p1[1], p2[1]))
            cv.wires.append((p1, p2))
            cv.wire_width[(p1, p2)] = _stroke_width_um(w)

    for s in sch.shapes:
        # `(rectangle)`/`(circle)`/`(arc)` — KiCad >= v7 schematic shapes
        # tool, same purely decorative role as `polyline` above (never
        # electrical) — direct <line>/<shape>/<arc> children of
        # <schematic>, GRAPHIC layer (ir_schema.md "Декоративная
        # геометрия схемы"). Filled follows the SAME semantics as the
        # symbol-body branches in kicad_parser._add_unit_geometry:
        # 'outline' = filled with the LINE color (real solid), 'color' =
        # filled with an explicit color (solid; IR has no color), 'none' =
        # not filled, 'background' = the canvas-color trap (looks unfilled
        # on screen) — the first version of this loop had the test
        # literally INVERTED (`!= 'outline'`), turning every unfilled
        # rectangle solid (found by the user on Pocket-Lab-Bench-Power).
        # Coordinates go through _pt (raw, un-inverted Y) same as wires/
        # junctions above — the Y-flip happens once, at XML-write time.
        tag = type(s).__name__
        filled = getattr(getattr(s, 'fill', None), 'type', None) in ('outline', 'color')
        width_um = int(_stroke_width_um(s, ctx['graphic_default_um']))
        if tag == 'Rectangle':
            x1, y1 = _pt(s.start.X + dx, s.start.Y)
            x2, y2 = _pt(s.end.X + dx, s.end.Y)
            stray_check(f'rectangle {getattr(s, "uuid", "")}',
                         min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2))
            # Unfilled -> a real <shape outline=w> contour, NOT four loose
            # <line>s (the old decomposition was lossy for no reason: the
            # Eagle exporter already decomposes outlined shapes into wires
            # itself, and the KiCad exporter has a native unfilled rect).
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            cv.deco_shapes.append(('rect', cx, cy, abs(x2 - x1), abs(y2 - y1),
                                 0, 0 if filled else width_um))
        elif tag == 'Circle':
            cx, cy = _pt(s.center.X + dx, s.center.Y)
            r_um = round(float(s.radius) * 1000)
            stray_check(f'circle {getattr(s, "uuid", "")}',
                         cx - r_um, cx + r_um, cy - r_um, cy + r_um)
            cv.deco_shapes.append(('circle', cx, cy, r_um * 2, r_um * 2,
                                 100, 0 if filled else width_um))
        elif tag == 'Arc':
            # IR arc canon = endpoints + curve (ir_util's arc-math block):
            # KiCad's own start/end ARE the endpoints, they pass through
            # exactly; the 3-point fit only supplies the signed bulge angle.
            # Points go in already in IR's Y-up space, dx pre-added — a pure
            # X-translation doesn't affect the sweep.
            params = _arc_params((s.start.X + dx, -s.start.Y), (s.mid.X + dx, -s.mid.Y),
                                  (s.end.X + dx, -s.end.Y))
            if params is None:
                continue
            cx, cy_ir, ar = params[0], params[1], params[2]
            x1_um, y1_um = round((s.start.X + dx) * 1000), round(-s.start.Y * 1000)
            x2_um, y2_um = round((s.end.X + dx) * 1000), round(-s.end.Y * 1000)
            stray_check(f'arc {getattr(s, "uuid", "")}',
                         round((cx - ar) * 1000), round((cx + ar) * 1000),
                         round((-cy_ir - ar) * 1000), round((-cy_ir + ar) * 1000))
            cv.deco_arcs.append((x1_um, y1_um, x2_um, y2_um, params[4], width_um))

    for t in sch.texts:
        # Free-standing `(text)` canvas notes — purely decorative, GRAPHIC
        # layer, same IR home as the rest of the decorative geometry
        # (previously dropped silently — worse than a hard fail). Rotation
        # is a literal copy, same as labels (decisions.md "KiCad: `ir_rot`
        # — БЕЗ инверсии"); multi-line text (\n) carries through as-is.
        tp = _pt(t.position.X + dx, t.position.Y)
        stray_check(f'text "{(t.text or "")[:20]}"', tp[0], tp[0], tp[1], tp[1])
        cv.deco_texts.append((tp[0], tp[1], t.text or '',
                               _text_size_um(t.effects),
                               (t.position.angle or 0) % 360,
                               _align(t.effects.justify if t.effects else None)))

    for tb in sch.textBoxes:
        # `(text_box)` (KiCad v7+) -> IR <note> (ir_schema.md "<note> —
        # markdown-заметка схемы"): the box carries exactly what a note
        # needs — raw text + a width — and the auto-wrap is done by OUR
        # renderer with OUR metrics, so KiCad's line breaking never needs
        # guessing at all. Frame/fill of the source box are dropped (IR
        # has no color; a note is drawn by editor convention).
        x1, y1 = _pt(tb.position.X + dx, tb.position.Y)
        w_um = round(float(tb.size.X) * 1000)
        h_um = round(float(tb.size.Y) * 1000)
        stray_check(f'text_box "{(tb.text or "")[:20]}"',
                     x1, x1 + w_um, y1, y1 + h_um)
        cv.deco_notes.append((x1, y1, w_um, tb.text or '',
                               (tb.position.angle or 0) % 360))

    for fl in sch.netclassFlags:
        # netclass_flag directive (KiCad v7+) — an on-wire class assignment.
        # Not yet supported (no real-world fixture yet; the pattern-based
        # assignment from .kicad_pro IS supported — see convert_project_full)
        # — logged, never dropped silently: this loses a class assignment.
        import_log.log(sch_path.name, getattr(fl, 'uuid', '') or '(no uuid)',
                        'NETCLASS_FLAG directive not yet supported, '
                        'class assignment dropped')

    for img in sch.images:
        # Raster `(image)` on the canvas — no IR entity, by decision: pure
        # color decoration (IR has no color), and Eagle schematics can't
        # hold rasters at all, so there'd be nowhere to export it. Logged,
        # never dropped silently; if rasters ever become worth modeling,
        # that decision rides along with the BOARD-side image mechanism
        # (decisions.md "Изображения на плате" — potrace at Gerber export),
        # with the model3d sidecar-file pattern ready for storage.
        import_log.log(sch_path.name, getattr(img, 'uuid', '') or '(no uuid)',
                        'SCHEMATIC_IMAGE dropped, not modeled')

    for j in sch.junctions:
        jp = _pt(j.position.X + dx, j.position.Y)
        stray_check('junction', jp[0], jp[0], jp[1], jp[1])
        cv.junction_points.add(jp)

    # Local/global labels are both treated as plain net-naming labels —
    # every page here is an independent KiCad top-level sheet (see
    # _detect_pages), so there's no module boundary for "global" to
    # mean anything different from "local" (ir_schema.md "Scope
    # label": global vs local is purely about which canvas/file a
    # label sits in). Cross-page links are plain `global_label`s —
    # confirmed real (testData/vimdrones.zip: 142 global / 0 local) —
    # matching text across pages unites them via the SAME by-name
    # grouping _build_nets already does within one page.
    # `hierarchicalLabels` is always empty here (sch.sheets is too —
    # enforced in _detect_pages) but harmless to include for symmetry.
    for l in list(sch.labels) + list(sch.globalLabels) + list(sch.hierarchicalLabels):
        lp = _pt(l.position.X + dx, l.position.Y)
        # Label text extent isn't checked (no font metrics available
        # here) — only its anchor point, same scope limitation as
        # everywhere else in this project that places text by origin
        # alone (ir_schema.md doesn't model rendered text bbox).
        stray_check(f'label "{l.text}"', lp[0], lp[0], lp[1], lp[1])
        # ir_schema.md "<label>" `style` — presentation-only, not
        # electrical. Only global/hierarchical labels carry KiCad's own
        # `shape` at all (plain `(label ...)` — LocalLabel — has no such
        # field, always plain text -> `crummy`). `tri_state` has no
        # direct equivalent among our 5 styles — maps to `bidir`
        # (closest visually, per the user) rather than inventing a 6th.
        style = _LABEL_STYLE.get(getattr(l, 'shape', None), 'crummy')
        # sch_path.name + raw (pre-dx) mm coordinates travel alongside
        # purely so a conflicting-label error (_build_nets) can point
        # the user at the exact sheet/position to fix, in KiCad's own
        # on-screen coordinates — not used for any geometry/connectivity
        # decision, which stays in the tiled dx-shifted space as before.
        cv.label_records.append((lp, l.text, l.position.angle, l.effects, style,
                               sch_path.name, l.position.X, l.position.Y))


def _finish_nets(cv):
    """Connectivity for one canvas — glue between the accumulated points
    and kicad_schematic._build_nets (see there for all the rules)."""
    label_points = [(p, text) for p, text, *_ in cv.label_records]
    # (point, text) -> "sheet.kicad_sch @ (x,y)mm" in KiCad's own on-screen
    # coordinates (pre-dx, pre-Y-flip) — only consulted by _build_nets when
    # it needs to name-and-shame a conflicting-label net (see there).
    label_origin = {(p, text): f'{sheet} @ ({x:g}, {y:g})mm'
                     for p, text, _, _, _, sheet, x, y in cv.label_records}
    return _build_nets(cv.wires, cv.junction_points, cv.pin_points,
                        label_points, label_origin)


def _write_canvas(parent_el, cv, nets, wire_default_um=152, wire_um_by_class=None):
    """Accumulated canvas content -> IR XML children of `parent_el` —
    either <schematic> or <module>, same content model (ir_schema.md
    "Модуль": внутри — та же структура, что у <schematic>).

    Wire width 0 (KiCad "use the net class default") is materialized HERE,
    where the net's resolved class is known: class wire_width if the class
    has one, the Default class's otherwise (see convert_project_full's
    default-widths block). Module nets get their class assigned after this
    runs — those resolve at the Default width, logged there if the class
    width actually differs."""
    wire_um_by_class = wire_um_by_class or {}
    for inst in cv.instances:
        if inst.get('module'):
            # Module instance (ir_schema.md "Модуль"): `module` instead of
            # `component`, no library/gate/footprint — block geometry and
            # port positions are derived from the <module> definition by
            # name, never stored again on the instance.
            inst_el = ET.SubElement(parent_el, 'instance', module=inst['module'],
                                     name=inst['designator'],
                                     x=inst['x'], y=inst['y'],
                                     rot=inst['rot'], mirror=inst['mirror'])
        else:
            inst_el = ET.SubElement(parent_el, 'instance', component=inst['component'],
                                     library=inst['library'], name=inst['designator'],
                                     x=inst['x'], y=inst['y'], rot=inst['rot'], mirror=inst['mirror'])
        if inst.get('gate'):
            inst_el.set('gate', inst['gate'])
        if inst.get('footprint'):
            inst_el.set('footprint', inst['footprint'])
        # Assembly flag, written only when non-default (yes is implied —
        # ir_schema.md "Варианты сборки", base-configuration flag).
        if inst.get('populate'):
            inst_el.set('populate', inst['populate'])
        for k, v in inst['attrs']:
            ET.SubElement(inst_el, 'attr', name=k, value=v)
        for tkw in inst.get('texts', ()):
            t = ET.SubElement(inst_el, 'text')
            t.text = tkw.pop('_text')
            for k, v in tkw.items():
                t.set(k, v)

    label_geom = {(p, text): (angle, effects, style)
                   for p, text, angle, effects, style, *_ in cv.label_records}

    for net in nets:
        net_el = ET.SubElement(parent_el, 'net', name=net['name'])
        if net.get('class'):
            net_el.set('class', net['class'])
        for seg in net['segments']:
            seg_el = ET.SubElement(net_el, 'segment')
            for designator, pin_name in seg['pinrefs']:
                if (designator, pin_name) in cv.portref_keys:
                    # <portref part=... port=...> — the parent-side
                    # attachment of a module instance's port (mirrors
                    # Eagle's <portref moduleinst= port=>, eagle.dtd;
                    # `part` matches <pinref>'s own attribute naming). The
                    # port behaves like a component pin for connectivity —
                    # a wire end on its position connects — but never names
                    # the parent net (naming stays with labels/sup pins).
                    ET.SubElement(seg_el, 'portref', part=designator, port=pin_name)
                else:
                    ET.SubElement(seg_el, 'pinref', part=designator, pin=pin_name)
            for p1, p2 in seg['wires']:
                (x1, y1), (x2, y2) = p1, p2
                w = cv.wire_width.get((p1, p2), '0')
                if not float(w):
                    w = str(wire_um_by_class.get(net.get('class'), wire_default_um))
                ET.SubElement(seg_el, 'line', x1=str(x1), y1=str(-y1), x2=str(x2), y2=str(-y2),
                              width=w)
            for jx, jy in seg['junctions']:
                ET.SubElement(seg_el, 'junction', x=str(jx), y=str(-jy))
            for (lx, ly), text in seg['labels']:
                angle, effects, style = label_geom.get(((lx, ly), text), (0, None, 'crummy'))
                # layer='NETS' (ir_schema.md "Слои" -> "Источник -> слой:
                # KiCad"): not currently READ by any consumer —
                # eagle_exporter.py uses its own _LYR_INFO constant directly
                # (decisions.md "KiCad: `ir_rot` — БЕЗ инверсии"/label layer
                # choice), svg_renderer.py uses its own _NET_LABEL color/
                # style directly too. Left as-is, not wired up — a real
                # per-consumer layer choice would need its own check, same
                # discipline as the rotation fix below.
                #
                # CONFIRMED (visual check in real Eagle, user): was
                # `_rot_fp(angle)` back when _rot_fp still negated — same
                # negate-the-angle formula disproven for schematic SYMBOL
                # rotation (decisions.md "KiCad: `ir_rot` — БЕЗ инверсии").
                # _rot_fp itself has since been ground-truthed the same way
                # (testData/rtfp, rotated pad, real KiCad vs render) and no
                # longer negates either — every rotation path is now
                # uniformly no-negation.
                ET.SubElement(seg_el, 'label', x=str(lx), y=str(-ly),
                              size=_text_size_um(effects), rot=_f((angle or 0) % 360),
                              layer='NETS', style=style)

    # Decorative canvas geometry (ir_schema.md "Декоративная геометрия
    # схемы") — direct children of the canvas element, GRAPHIC layer, no
    # electrical meaning, never fed into _build_nets. deco_lines/
    # deco_shapes store raw KiCad Y-down mm (flipped once here, same
    # convention as <net>'s own <line>/<junction>/<label> above); deco_arcs
    # stores final IR-space (Y-up) values already (see the Arc branch in
    # _collect_canvas for why).
    for x1, y1, x2, y2, width_um in cv.deco_lines:
        ET.SubElement(parent_el, 'line', x1=str(x1), y1=str(-y1), x2=str(x2), y2=str(-y2),
                      width=str(width_um), layer='GRAPHIC')
    for kind, cx, cy, w, h, roundness, outline_um in cv.deco_shapes:
        ET.SubElement(parent_el, 'shape', x=str(cx), y=str(-cy), w=str(w), h=str(h),
                      roundness=str(roundness), outline=str(outline_um), rot='0', layer='GRAPHIC')
    for x1_um, y1_um, x2_um, y2_um, curve, width_um in cv.deco_arcs:
        ET.SubElement(parent_el, 'arc', x1=str(x1_um), y1=str(y1_um),
                      x2=str(x2_um), y2=str(y2_um),
                      curve=_f(curve), width=str(width_um), layer='GRAPHIC')
    for tx, ty, text, size_um, rot, align in cv.deco_texts:
        t_el = ET.SubElement(parent_el, 'text', x=str(tx), y=str(-ty),
                             size=size_um, rot=_f(rot), align=align, layer='GRAPHIC')
        t_el.text = text
    for nx, ny, w_um, md, rot in cv.deco_notes:
        n_el = ET.SubElement(parent_el, 'note', x=str(nx), y=str(-ny),
                             w=str(w_um), rot=_f(rot))
        n_el.text = md


# ---------------------------------------------------------------------------
# Full project import (.swprj): merged copied pool + <schematic> canvas
# ---------------------------------------------------------------------------

def convert_project_full(src, output_path):
    """Single project -> one .swprj: a `<project>` carrying the full COPIED
    pool (symbols + components — same shape a .swlib would have, per the
    user: libraries stay a separate thing, but every component the design
    actually uses is copied into the project file whole — symbol,
    footprint, and pin-mapping) plus a `<schematic>` section with placed
    `<instance>`s and `<net>`s.

    Handles both a single-sheet project and KiCad's own flat multi-page
    mechanism — see _detect_pages and decisions.md "KiCad flat multi-page:
    top_level_sheets, не sheet-символ". Each extra page contributes its own
    `<instance>`s, wires/labels, AND a synthesized "Frame" component
    (ir_schema.md "Frame") from its `paper`/`title_block`, tiled in a
    horizontal row (ir_schema.md "Импорт страниц источника", 0.5" gap) —
    same-named `global_label`s across pages join into one <net>
    automatically, via the SAME by-label-name grouping _build_nets already
    does for same-named labels on one page (see decisions.md).

    Real hierarchy: depth-1 `(sheet)` symbols become IR <module>s
    (ir_schema.md "Модуль (design block + иерархия)"; ground truth
    testData/multichannel — channel_strip.kicad_sch x4) — one <module>
    definition per unique sheet FILE, one <instance module=...> per (sheet)
    occurrence, ports from the sheet pins (_collect_modules/_sheet_ports).
    Anything nested deeper than one level is still a hard reject —
    hierarchy is never flattened, full stop.
    """
    project_dir, pro_path = _find_project_root(src)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pcb_path = project_dir / f'{pro_path.stem}.kicad_pcb'

    embedded_dir = output_path.parent / f'{output_path.stem}_embedded_raw'
    embedded_names = _extract_embedded_models(pcb_path, embedded_dir)

    sym_lib_table = None
    sym_lib_table_path = project_dir / 'sym-lib-table'
    if sym_lib_table_path.exists():
        sym_lib_table = LibTable.from_file(str(sym_lib_table_path), encoding='utf-8')
    fp_lib_table = None
    fp_lib_table_path = project_dir / 'fp-lib-table'
    if fp_lib_table_path.exists():
        fp_lib_table = LibTable.from_file(str(fp_lib_table_path), encoding='utf-8')
    sym_lib_table, fp_lib_table = _augment_lib_tables(project_dir, sym_lib_table, fp_lib_table)

    pages = _detect_pages(pro_path, project_dir)
    modules, sheet_uses = _collect_modules(pages, project_dir)

    for _, sch in pages:
        if sch.busEntries:
            raise ValueError(f'{pro_path.stem}: bus entries found in the schematic — '
                              f'not yet supported.')

    # Resolve every used symbol/footprint fresh from the project's own
    # libraries — same approach convert_project uses, just merged into one
    # pool instead of split by nickname (see module docstring point 2/3 for
    # why "fresh from the library" instead of trusting cached copies, and
    # the hard-fail rationale on the resolution loops below). Across ALL
    # pages — a component can be placed on any of them.
    footprint_refs_by_libid = {}
    # ...across ALL canvases — top-level pages AND module files (a component
    # placed only inside a module still needs its pool entry).
    for sch in [s for _, s in pages] + [msch for _, msch in modules.values()]:
        for sym in sch.schematicSymbols:
            lib_id = f'{sym.libraryNickname}:{sym.entryName}' if sym.libraryNickname else sym.entryName
            fp_prop = next((p for p in sym.properties if p.key == 'Footprint'), None)
            fp_ref = fp_prop.value if fp_prop and not _kicad_blank(fp_prop.value) else None
            bucket = footprint_refs_by_libid.setdefault(lib_id, set())
            if fp_ref:
                bucket.add(fp_ref)

    sym_lib_cache = {}
    template_by_libid = {}
    for lib_id in footprint_refs_by_libid:
        sym = _resolve_symbol(lib_id, project_dir, sym_lib_table, sym_lib_cache)
        if sym is None:
            raise ValueError(
                f'{lib_id}: symbol not found in any library registered in '
                f'sym-lib-table. Export it (KiCad: "Export Symbols to New '
                f'Library...") and include that library in the project '
                f'archive before converting.')
        template_by_libid[lib_id] = sym

    # Per-instance power Value renames (a legal KiCad idiom: place GND,
    # set its Value to AGND — the INSTANCE Value names the net). IR models
    # exactly one net name per supply symbol (the sup PIN's name), so each
    # distinct Value becomes its own cloned component — an unambiguous
    # split, not a guess and not a reject (found on testData/multichannel:
    # +1V1 placed with Value "vbias").
    power_value_virt = {}          # (lib_id, instance Value) -> virtual lib_id
    power_default_used = set()     # power lib_ids with >=1 default-Value instance
    def _tpl_value(tpl):
        p = next((q for q in tpl.properties if q.key == 'Value'), None)
        return p.value if p else ''
    for sch in [s for _, s in pages] + [msch for _, msch in modules.values()]:
        for sym in sch.schematicSymbols:
            lib_id = f'{sym.libraryNickname}:{sym.entryName}' if sym.libraryNickname else sym.entryName
            tpl = template_by_libid.get(lib_id)
            if tpl is None or not tpl.isPower or _is_erc_power_source(tpl):
                continue
            v = next((p.value for p in sym.properties if p.key == 'Value'), None)
            if not v or v == _tpl_value(tpl):
                power_default_used.add(lib_id)
                continue
            if (lib_id, v) in power_value_virt:
                continue
            clone = copy.deepcopy(tpl)
            old_entry = clone.entryName
            suffix = clean_attr_name(_kicad_overbar_to_eagle(v))
            clone.entryName = f'{old_entry}@{suffix}'
            for u_ in clone.units:
                if (u_.entryName or '').startswith(old_entry):
                    u_.entryName = clone.entryName + u_.entryName[len(old_entry):]
            vp = next((p for p in clone.properties if p.key == 'Value'), None)
            if vp is not None:
                vp.value = v
            virt = f'{lib_id}@{suffix}'
            template_by_libid[virt] = clone
            footprint_refs_by_libid[virt] = set()
            power_value_virt[(lib_id, v)] = virt
            import_log.log(lib_id, v,
                           f'POWER Value rename -> dedicated component '
                           f'{clone.entryName!r} (net = per-instance Value)')
    # A power symbol whose EVERY placement was renamed leaves no instance
    # on the base component — drop it, or the pool grows a phantom entry
    # (caught by roundtrip_kicad: IR2 rightfully has no unused '+1V1').
    for lib_id in {k for k, _ in power_value_virt} - power_default_used:
        del template_by_libid[lib_id]
        del footprint_refs_by_libid[lib_id]

    fp_cache = {}
    groups = {}   # lib_id -> {footprint_ref: Footprint}
    for lib_id, fp_refs in footprint_refs_by_libid.items():
        for fp_ref in fp_refs:
            fp_path = _resolve_footprint_path(fp_ref, project_dir, fp_lib_table)
            if fp_path is None:
                raise ValueError(
                    f'{fp_ref}: footprint not found in any library registered '
                    f'in fp-lib-table. Export it (KiCad: "Export Footprints to '
                    f'New Library...") and include that library in the '
                    f'project archive before converting.')
            if fp_path not in fp_cache:
                fp_cache[fp_path] = _read_footprint(fp_path)
            groups.setdefault(lib_id, {})[fp_ref] = fp_cache[fp_path]

    # Merged pool: one <component> per distinct lib_id, bare entryName
    # (nickname-suffixed only on a real name collision between two
    # DIFFERENT lib_ids — unlike convert_project's per-nickname .swlib
    # split, everything lands in one document here, so collisions are
    # possible even though they weren't before).
    proj_el = ET.Element('project', name=pro_path.stem)
    symbols_el = ET.SubElement(proj_el, 'symbols')
    pool = {}
    out_models_dir = output_path.parent / output_path.stem

    comp_name_by_libid = {}
    comp_attrs_by_libid = {}
    gates_by_libid = {}
    gate_letter_by_libid = {}   # lib_id -> {kicad unitId: gate letter}, ir_schema.md "Размещение многорежимного компонента"
    library_by_libid = {}
    name_owner = {}
    for lib_id, sym_template in template_by_libid.items():
        info = _convert_schematic_symbol(sym_template, pool, symbols_el)
        gate_letter_by_libid[lib_id] = _unit_id_to_gate_letter(sym_template)
        nickname, entry = _nickname_and_entry(lib_id, pro_path.stem)
        comp_name = entry
        if name_owner.get(comp_name, lib_id) != lib_id:
            comp_name = f'{nickname}_{entry}'
        name_owner[comp_name] = lib_id
        comp_name_by_libid[lib_id] = comp_name
        gates_by_libid[lib_id] = info['gates']
        library_by_libid[lib_id] = nickname

        comp_el = ET.Element('component', name=comp_name, prefix=info['prefix'])
        gates = info['gates']
        if len(gates) == 1 and gates[0][0] is None:
            comp_el.set('symbol', gates[0][1])
        else:
            for letter, sym_name, _ in gates:
                ET.SubElement(comp_el, 'gate', name=letter, symbol=sym_name)

        for fp in groups.get(lib_id, {}).values():
            fp_name = fp.entryName
            fp_el, pad_name_groups = _convert_footprint(fp, fp_name)
            _copy_model3d(fp, fp_el, embedded_dir if embedded_names else None, out_models_dir, fp_name)

            comp_fp = ET.SubElement(comp_el, 'footprint', name=fp_name)
            for child in fp_el:
                comp_fp.append(child)
            pm_el = ET.SubElement(comp_fp, 'pin-mapping')
            if len(gates) == 1 and gates[0][0] is None:
                _, _, named_pins = gates[0]
                for ir_name, pad_num in _pin_mapping(named_pins, pad_name_groups):
                    ET.SubElement(pm_el, 'map', pin=ir_name, pad=pad_num)
            else:
                for letter, _, named_pins in gates:
                    for ir_name, pad_num in _pin_mapping(named_pins, pad_name_groups):
                        ET.SubElement(pm_el, 'map', pin=f'{letter}.{ir_name}', pad=pad_num)

        if info['attrs']:
            attrs_el = ET.SubElement(comp_el, 'attributes')
            for k, v in info['attrs']:
                ET.SubElement(attrs_el, 'attr', name=k, value=v)
        comp_attrs_by_libid[lib_id] = dict(info['attrs'] or [])

        proj_el.append(comp_el)

    ctx = {'pool': pool, 'gates_by_libid': gates_by_libid,
           'gate_letter_by_libid': gate_letter_by_libid,
           'comp_name_by_libid': comp_name_by_libid,
           'library_by_libid': library_by_libid,
           'comp_attrs_by_libid': comp_attrs_by_libid,
           'power_value_virt': power_value_virt,
           'groups': groups}

    # --- Net classes (ir_schema.md "Net class"): definitions -> <classes>,
    # assignment patterns resolved to explicit class= attributes at the end
    # of this function (they need the final nets + module instance names).
    pro_json = json.loads(Path(pro_path).read_text(encoding='utf-8'))
    net_settings = pro_json.get('net_settings') or {}

    # --- Default stroke widths, resolved AT IMPORT (ir_schema.md: width is
    # an explicit µm fact on every <line>; "0 = tool default" must not leak
    # into the IR — previously each exporter re-invented the default on its
    # own: eagle_exporter substituted its _WIRE_W constant, the KiCad
    # exporter wrote 0 back and real KiCad happened to re-render it right;
    # two independent defaults agreeing by luck). KiCad semantics: a wire
    # with stroke width 0 renders at its NET CLASS's wire_width (Default
    # class = 6 mil unless overridden); canvas graphics at
    # schematic.drawing.default_line_thickness. Both .kicad_pro fields are
    # in MILS (unlike the pcb fields right next to them, which are mm).
    _MIL_UM = 25.4
    drawing = (pro_json.get('schematic') or {}).get('drawing') or {}
    graphic_default_um = round(float(drawing.get('default_line_thickness') or 6) * _MIL_UM)
    wire_default_um = round(6 * _MIL_UM)
    wire_um_by_class = {}
    for c in (net_settings.get('classes') or []):
        if c.get('wire_width') is None or not c.get('name'):
            continue
        um = round(float(c['wire_width']) * _MIL_UM)
        if c['name'] == 'Default':
            wire_default_um = um
        else:
            wire_um_by_class[c['name']] = um
    ctx['graphic_default_um'] = graphic_default_um
    nc_patterns = net_settings.get('netclass_patterns') or []
    if net_settings.get('netclass_assignments'):
        import_log.log('net_settings', 'netclass_assignments',
                        'NETCLASS assignments present — not yet supported, ignored')
    nc_defs = [c for c in (net_settings.get('classes') or [])
               if c.get('name') and c['name'] != 'Default']
    if nc_defs:
        classes_el = ET.SubElement(proj_el, 'classes')
        for c in nc_defs:
            cl = ET.SubElement(classes_el, 'class', name=c['name'])
            # Board triple only (ir_schema.md "Net class"): KiCad's class
            # is a sparse overlay over Default — copy what's actually set;
            # schematic cosmetics (wire_width/colors/line_style) are not
            # carried (no color in IR, wire widths already live on <line>).
            for src, dst in (('track_width', 'width'), ('via_drill', 'drill'),
                              ('clearance', 'clearance')):
                if c.get(src) is not None:
                    cl.set(dst, _um(c[src]))

    # --- Modules: each unique (sheet) file -> one <module> child of
    # <project>, BEFORE <schematic> (ir_schema.md "Модуль (design block +
    # иерархия)"). Content goes through the SAME canvas machinery as a
    # top-level page (_collect_canvas), just into its own accumulator/net
    # space — a module is its own canvas, port names are the only link to
    # the parent. No tiling dx (module is not part of the page row), but
    # the module file's own paper DOES become a synthesized Frame + stray
    # validation, same as a page (see inside the loop).
    module_cvs = []
    synth_ports_by_stem = {}   # stem -> [(name, direction, side, coord_um_str)]
    mod_el_by_stem = {}
    module_net_names_by_stem = {}
    port_names_by_stem = {}
    for stem, (mod_path, mod_sch) in modules.items():
        uses = [s for _, s, st in sheet_uses if st == stem]
        first = uses[0]
        ports = _sheet_ports(first)
        # Every occurrence of the SAME module file must have the IDENTICAL
        # port layout — same pins, same sides, same coords, same block size.
        # We deliberately do NOT try to recover a per-occurrence rot/mirror
        # from a diverging layout: KiCad lets a "rotated" sheet re-lay its
        # pins out INCORRECTLY (confirmed by the user rotating a sheet by
        # hand — one pin, +1V1, did NOT travel with the block), and KiCad
        # also allows deleting/moving a single sheet pin per occurrence.
        # Both are exactly the "same block, silent local differences" anti-
        # pattern that makes KiCad schematics unmaintainable (see
        # [[babel_purpose_kicad_exit]] / decisions.md "Требуем нормальности"):
        # importing them would bake a KiCad defect into the IR. So a
        # divergence is a HARD REJECT, naming the offending instance and
        # port — the user must make the module instances uniform (or use a
        # tool that rotates a hierarchical sheet honestly) and re-export.
        # (Rotated module INSTANCES still round-trip the OTHER way — IR->KiCad
        # export bakes rot/mirror faithfully, ir_util.rotate_port_side; it's
        # only KiCad-as-a-SOURCE of rotated sheets we refuse, because KiCad's
        # own rotation is unreliable.)
        for other in uses[1:]:
            if (_sheet_ports(other) != ports
                    or (other.width, other.height) != (first.width, first.height)):
                diff = _describe_sheet_divergence(ports, first, other)
                raise ValueError(
                    f'module "{stem}": instance '
                    f'"{other.sheetName.value or "(unnamed)"}" has a DIFFERENT sheet-pin '
                    f'layout than instance "{first.sheetName.value or "(unnamed)"}" of the '
                    f'same module file ({diff}). The same module must look identical at '
                    f'every placement — KiCad allows per-instance pin edits (and rotates '
                    f'hierarchical sheets unreliably), which is exactly the silent-'
                    f'divergence anti-pattern Babel refuses to import. Make every instance '
                    f'of "{stem}" uniform and re-export.')
        mod_el = ET.SubElement(proj_el, 'module', name=stem,
                                dx=_um(first.width), dy=_um(first.height))
        for pname, pdir, pside, pcoord in ports:
            ET.SubElement(mod_el, 'port', name=pname, direction=pdir,
                          side=pside, coord=pcoord)
        cv = _Canvas()
        # Module canvas gets its own page frame after all (revised — was
        # "a design block is not a printable page"): the module FILE has a
        # real paper size + title_block exactly like a top-level page, and
        # the user wants the module canvas printable/framed like any other.
        # Same synthesis and stray validation as the page loop below, just
        # without tiling (dx=0, module is its own canvas).
        width_mm, height_mm = _paper_dims_mm(mod_sch.paper)
        frame_x1, frame_y1 = round(width_mm * 1000), round(height_mm * 1000)
        strays = []

        def _stray_check(label, x0, x1, y0, y1, _fx1=frame_x1, _fy1=frame_y1):
            if x0 < 0 or x1 > _fx1 or y0 < 0 or y1 > _fy1:
                strays.append(f'{label}: bbox x[{x0/1000:g},{x1/1000:g}] '
                              f'y[{y0/1000:g},{y1/1000:g}] vs frame '
                              f'x[0,{_fx1/1000:g}] y[0,{_fy1/1000:g}]')

        _collect_canvas(mod_sch, mod_path, 0.0, cv, ctx, _stray_check)
        if strays:
            raise ValueError(
                f'{mod_path.name}: {len(strays)} object(s) extend outside the module page '
                f'frame ({width_mm:g}x{height_mm:g}mm) — every object must fit entirely '
                f'inside the printable frame (decisions.md "Stray-валидация"):\n  ' +
                '\n  '.join(strays))

        frame_comp_name = _build_frame_component(width_mm, height_mm, pool, symbols_el, proj_el)
        tb = mod_sch.titleBlock
        frame_attrs = []
        if tb is not None:
            for key, val in (('title', tb.title), ('company', tb.company),
                              ('rev', tb.revision), ('date', tb.date)):
                if val:
                    frame_attrs.append((key, val))
        cv.instances.append({
            'designator': 'FRAME1',
            'component': frame_comp_name,
            'library': '',
            'x': _um(width_mm / 2), 'y': _um(-(height_mm / 2)),
            'rot': '0', 'mirror': '0',
            'attrs': frame_attrs,
        })

        mod_nets = _finish_nets(cv)
        net_names = {n['name'] for n in mod_nets}
        for pname, *_ in ports:
            # Port <-> inner net link is by NAME only (ir_schema.md
            # "Модуль") — a port with no same-named net inside the module
            # is dead (hierarchical label missing/renamed in the file).
            if pname not in net_names:
                import_log.log(stem, pname,
                                'MODULE_PORT has no matching net inside the module')

        # --- Synthesized ports: KiCad supply symbols and global_labels are
        # GLOBAL in the source — they silently cross the sheet boundary and
        # merge with same-named nets anywhere in the project (supply
        # symbols got a local option only in KiCad 10). Our scope model
        # forbids that implicit leakage (ir_schema.md "Scope label": внутри
        # модуля понятия глобальной метки нет, питание наружу — только
        # явный порт), so the source's implicit connectivity is made
        # EXPLICIT once, at import: every such net becomes a real <port> on
        # the module definition, and every instance gets a wire stub +
        # label + <portref> on the parent canvas (see the page loop) — the
        # label's own by-name merge in _build_nets then joins it with the
        # parent's same-named net, which is exactly the source semantics,
        # now spelled out. Placement: least-crowded side (_alloc_port_slot).
        # Only GLOBAL supplies leak across the sheet boundary and need an
        # explicit port. A `(power local)` supply (KiCad 10) is a
        # deliberately isolated net — synthesizing a port for it would be
        # WRONG (the plan's import-side rule); logged per net below.
        sup_names = set(cv.global_sup_names)
        for lname in sorted(cv.local_sup_names - cv.global_sup_names):
            import_log.log(stem, lname,
                            'LOCAL_SUPPLY net stays inside the module, no port synthesized')
        global_shapes = {l.text: getattr(l, 'shape', None) for l in mod_sch.globalLabels}
        existing_port_names = {p[0] for p in ports}
        occupied = {'left': [], 'right': [], 'top': [], 'bottom': []}
        for _, _, pside, pcoord in ports:
            occupied[pside].append(int(pcoord))
        side = _synth_port_side(occupied)
        synth_ports = []
        for net_name in sorted(sup_names | set(global_shapes)):
            if net_name in existing_port_names or net_name not in net_names:
                continue
            direction = ('pwr' if net_name in sup_names
                         else _PORT_DIRECTION.get(global_shapes.get(net_name), 'io'))
            coord_um = _alloc_port_slot(occupied[side])
            synth_ports.append((net_name, direction, side, str(coord_um)))
            import_log.log(stem, net_name,
                            f'MODULE_PORT synthesized ({"supply" if net_name in sup_names else "global_label"})',
                            f'-> {side} @ {coord_um}um')
        for pname, pdir, pside, pcoord in synth_ports:
            ET.SubElement(mod_el, 'port', name=pname, direction=pdir,
                          side=pside, coord=pcoord)
        synth_ports_by_stem[stem] = synth_ports
        mod_el_by_stem[stem] = mod_el
        module_net_names_by_stem[stem] = net_names
        port_names_by_stem[stem] = ({p[0] for p in ports}
                                     | {p[0] for p in synth_ports})

        _write_canvas(mod_el, cv, mod_nets, wire_default_um, wire_um_by_class)
        module_cvs.append(cv)

    # --- Schematic canvas: placed instances + connectivity, across all pages ---
    cv_top = _Canvas()
    used_modinst_names = set()
    synth_tails = []   # (tail_end_pt, inst_name, port_name) — for the silent-merge guard below
    modinst_names_by_stem = {}   # stem -> [instance names] — for netclass pattern expansion

    # Horizontal-row tiling (ir_schema.md "Импорт страниц источника"): page
    # 1 (root) keeps its own native coordinates (x_shift=0); every next page
    # is pushed right by the PREVIOUS pages' paper widths + a 0.5" gap each
    # — using the real paper size now that we have one (KiCad gives this
    # structurally), not a computed content bbox (that was the fallback for
    # sources without a reliable frame/page size at all). X, not Y: X's
    # positive direction is the ONE convention KiCad/Eagle/IR all agree on
    # (Y flips sign between KiCad and IR) — tiling along X sidesteps having
    # to pick a sign for the shift at all (decisions.md "Импорт страниц
    # источника: X, не Y").
    x_shift_mm = 0.0
    page_shifts = []
    for _, sch in pages:
        page_shifts.append(x_shift_mm)
        x_shift_mm += _paper_dims_mm(sch.paper)[0] + _GAP_MM

    for page_idx, (sch_path, sch) in enumerate(pages):
        dx = page_shifts[page_idx]

        # Stray-component validation (decisions.md "Stray-валидация") — every
        # object on this page must fit ENTIRELY inside the page's own frame
        # (paper bbox), not just touch it. Computed in KiCad-native Y-down
        # µm-int space (same `_pt`-style rounding already used for wires/
        # junctions/labels below) so every check below is a plain integer
        # range comparison, no unit juggling per object.
        width_mm, height_mm = _paper_dims_mm(sch.paper)
        frame_x0, frame_x1 = round(dx * 1000), round((dx + width_mm) * 1000)
        frame_y0, frame_y1 = 0, round(height_mm * 1000)
        strays = []

        def _stray_check(label, x0, x1, y0, y1):
            if x0 < frame_x0 or x1 > frame_x1 or y0 < frame_y0 or y1 > frame_y1:
                strays.append(f'{label}: bbox x[{x0/1000:g},{x1/1000:g}] '
                              f'y[{y0/1000:g},{y1/1000:g}] vs frame '
                              f'x[{frame_x0/1000:g},{frame_x1/1000:g}] '
                              f'y[{frame_y0/1000:g},{frame_y1/1000:g}]')

        _collect_canvas(sch, sch_path, dx, cv_top, ctx, _stray_check)

        # (sheet) placements on this page -> <instance module=...> on the
        # top canvas + port connection points for parent-side nets. The
        # sheet pin behaves exactly like a component pin for connectivity
        # (a wire END on its position connects — same exact-coordinate rule
        # _build_nets applies to component pins) and never names the parent
        # net; portref_keys makes the writer emit <portref> instead of
        # <pinref> for these (ir_schema.md "Модуль").
        for sheet in sch.sheets:
            stem = Path(sheet.fileName.value).stem
            inst_name = (sheet.sheetName.value or '').strip() or f'MD{len(used_modinst_names) + 1}'
            base, k = inst_name, 2
            while inst_name in used_modinst_names:
                # Duplicate sheet names are legal in KiCad (identity lives
                # on the uuid) — IR module-instance names must be unique.
                inst_name = f'{base}_{k}'
                k += 1
            if inst_name != base:
                import_log.log(sch_path.name, base,
                                'MODULE_INSTANCE duplicate sheet name, renamed ->', inst_name)
            used_modinst_names.add(inst_name)
            modinst_names_by_stem.setdefault(stem, []).append(inst_name)
            x0_mm, y0_mm = sheet.position.X + dx, sheet.position.Y
            _stray_check(f'module instance {inst_name}',
                         round(x0_mm * 1000), round((x0_mm + sheet.width) * 1000),
                         round(y0_mm * 1000), round((y0_mm + sheet.height) * 1000))
            # rot/mirror are always 0 here: every occurrence of a module is
            # required to be IDENTICAL (hard-rejected in _collect_modules
            # otherwise), so KiCad never gives us a rotated one to recover.
            cv_top.instances.append({
                # x/y = the block rectangle's CENTER (ir_schema.md "Модуль":
                # origin — геометрический центр прямоугольника), Y-flipped
                # once at record time like every other instance here.
                'designator': inst_name, 'module': stem,
                'x': _um(x0_mm + sheet.width / 2),
                'y': _um(-(y0_mm + sheet.height / 2)),
                'rot': '0', 'mirror': '0', 'attrs': [],
            })
            for pin in sheet.pins:
                pp = _pt(pin.position.X + dx, pin.position.Y)
                cv_top.pin_points.append((pp, inst_name, pin.name, False))
                cv_top.portref_keys.add((inst_name, pin.name))

            # Synthesized (supply/global) ports of this module: the source
            # has NO parent-side geometry at all (the connection was
            # implicit/global), so a short wire stub + net-name label are
            # synthesized outward from each port. The label is what closes
            # the circuit: _build_nets' by-name merge joins it with the
            # parent's same-named net (or creates one) — same machinery,
            # no new connectivity code. Coordinates below are raw KiCad
            # Y-down mm: IR coord along a vertical edge is +up, hence the
            # minus; 'top' is the SMALLER raw y.
            cx_mm, cy_mm = x0_mm + sheet.width / 2, y0_mm + sheet.height / 2
            for pname, pdir, pside, pcoord in synth_ports_by_stem.get(stem, ()):
                coord_mm = int(pcoord) / 1000
                if pside == 'left':
                    px, py, ox, oy = cx_mm - sheet.width / 2, cy_mm - coord_mm, -1, 0
                elif pside == 'right':
                    px, py, ox, oy = cx_mm + sheet.width / 2, cy_mm - coord_mm, 1, 0
                elif pside == 'top':
                    px, py, ox, oy = cx_mm + coord_mm, cy_mm - sheet.height / 2, 0, -1
                else:
                    px, py, ox, oy = cx_mm + coord_mm, cy_mm + sheet.height / 2, 0, 1
                tx, ty = px + ox * _SYNTH_PORT_TAIL_MM, py + oy * _SYNTH_PORT_TAIL_MM
                p_pt, t_pt = _pt(px, py), _pt(tx, ty)
                _stray_check(f'module port stub {inst_name}.{pname}',
                             min(p_pt[0], t_pt[0]), max(p_pt[0], t_pt[0]),
                             min(p_pt[1], t_pt[1]), max(p_pt[1], t_pt[1]))
                cv_top.pin_points.append((p_pt, inst_name, pname, False))
                cv_top.portref_keys.add((inst_name, pname))
                cv_top.wires.append((p_pt, t_pt))
                cv_top.label_records.append((t_pt, pname, 0, None, 'crummy',
                                              sch_path.name, tx - dx, ty))
                synth_tails.append((t_pt, inst_name, pname))

        if strays:
            raise ValueError(
                f'{sch_path.name}: {len(strays)} object(s) extend outside the page frame '
                f'({width_mm:g}x{height_mm:g}mm) — every object must fit entirely inside '
                f'the printable frame (decisions.md "Stray-валидация"), not just touch it. '
                f'Move it inside the frame (or enlarge the page) and re-export:\n  ' +
                '\n  '.join(strays))

        # Page frame (ir_schema.md "Frame") — synthesized from paper +
        # title_block, one shared <component> per paper size, one
        # <instance> per page (Title/Date/Rev/Company are real PER-PAGE
        # values in KiCad — confirmed different date/rev on each page of
        # testData/pic_programmer.zip — so they're instance overrides, not
        # baked into the shared component).
        frame_comp_name = _build_frame_component(width_mm, height_mm, pool, symbols_el, proj_el)
        tb = sch.titleBlock
        frame_attrs = []
        if tb is not None:
            for key, val in (('title', tb.title), ('company', tb.company),
                              ('rev', tb.revision), ('date', tb.date)):
                if val:
                    frame_attrs.append((key, val))
        cv_top.instances.append({
            'designator': f'FRAME{page_idx + 1}',
            'component': frame_comp_name,
            'library': '',
            'x': _um(width_mm / 2 + dx), 'y': _um(-(height_mm / 2)),
            'rot': '0', 'mirror': '0',
            'attrs': frame_attrs,
        })

    # Top-level `(power local)` supplies: on a SINGLE page the distinction
    # is vacuous (nothing to merge with — treated as global, logged). On a
    # multi-page project it is inexpressible: KiCad scopes such a net to
    # one page file, while the IR top level is ONE canvas with same-name
    # merge (ir_schema.md «Листов как сущности нет») — importing it would
    # silently rejoin what the author explicitly isolated.
    if cv_top.local_sup_names:
        if len(pages) > 1:
            raise ValueError(
                f'top-level pages carry (power local) supply symbol(s) for '
                f'{sorted(cv_top.local_sup_names)} — a page-scoped supply net '
                f'cannot be represented on the IR\'s single merged canvas '
                f'(local supplies belong inside modules). Make them global '
                f'(or move that circuitry into a sheet) in KiCad and re-export.')
        for lname in sorted(cv_top.local_sup_names):
            import_log.log(pro_path.stem, lname,
                            'LOCAL_SUPPLY on the single top-level page — '
                            'treated as global (no second page to differ from)')

    # Silent-merge guard for synthesized port stubs: a stub END landing
    # exactly on some OTHER connection point (a component pin, another
    # instance's port, another wire's end) would fuse two nets by pure
    # coordinate coincidence. When the names differ, _build_nets'
    # conflicting-label check already hard-fails; but a SAME-name collision
    # would merge silently — and either way the stub was auto-placed by
    # us, not drawn by the user, so any such touch must be surfaced, never
    # kept quiet. Fix is the user's (move the blocks apart in KiCad).
    pinpt_set = {p for p, *_ in cv_top.pin_points}
    end_counts = {}
    for a, b in cv_top.wires:
        end_counts[a] = end_counts.get(a, 0) + 1
        end_counts[b] = end_counts.get(b, 0) + 1
    stub_conflicts = [
        f'{inst}.{pname}: stub end at ({t[0]/1000:g}, {t[1]/1000:g})mm (tiled canvas)'
        for t, inst, pname in synth_tails
        if t in pinpt_set or end_counts.get(t, 0) > 1]
    if stub_conflicts:
        raise ValueError(
            f'{len(stub_conflicts)} synthesized module-port stub(s) collide with other '
            f'connection points on the parent canvas — the auto-placed supply/global '
            f'port stub would touch someone else\'s pin/port/wire and silently merge '
            f'nets. Move the module instances further apart (or free the colliding '
            f'side) in KiCad and re-export:\n  ' + '\n  '.join(stub_conflicts))

    n_skipped_multi_unit = cv_top.n_skipped_multi_unit + sum(c.n_skipped_multi_unit for c in module_cvs)
    n_skipped_multi_gate = cv_top.n_skipped_multi_gate + sum(c.n_skipped_multi_gate for c in module_cvs)
    if n_skipped_multi_unit:
        print(f'  ! {n_skipped_multi_unit} placed symbol(s) use a non-1 unit '
              f'(multi-unit/gate canvas placement) — not yet supported, skipped.')
    if n_skipped_multi_gate:
        print(f'  ! {n_skipped_multi_gate} placed symbol(s) are multi-gate components '
              f'— per-gate canvas placement not yet supported, skipped.')

    schem_el = ET.SubElement(proj_el, 'schematic')
    nets = _finish_nets(cv_top)

    # --- Net class assignment (ir_schema.md "Net class"): the source's
    # wildcard patterns over HIERARCHICAL net names are expanded here into
    # explicit class= attributes — the mechanism is not carried, only its
    # result. Hierarchical candidates: '/NAME' for a top-level net,
    # '/INSTANCE/NAME' for a module net, once per placed instance.
    if nc_patterns:
        unused_patterns = {p['pattern'] for p in nc_patterns}
        nc_conflicts = []

        def _pattern_hits(hier_name):
            hits = set()
            for p in nc_patterns:
                if fnmatch.fnmatchcase(hier_name, p['pattern']):
                    unused_patterns.discard(p['pattern'])
                    # Explicit assignment INTO Default is a no-op, not a class.
                    if p['netclass'] != 'Default':
                        hits.add(p['netclass'])
            return hits

        top_hits = {}   # top net name -> set of class names
        for net in nets:
            h = _pattern_hits('/' + net['name'])
            if h:
                top_hits.setdefault(net['name'], set()).update(h)

        # (instance, port) -> the top-level net dict it attaches to — needed
        # for the boundary rule below (parent net owns name AND class).
        portref_net = {}
        for net in nets:
            for seg in net['segments']:
                for d, p in seg['pinrefs']:
                    if (d, p) in cv_top.portref_keys:
                        portref_net[(d, p)] = net

        for stem, mod_net_names in module_net_names_by_stem.items():
            insts = modinst_names_by_stem.get(stem, [])
            for nname in mod_net_names:
                per_inst = {i: _pattern_hits(f'/{i}/{nname}') for i in insts}
                if not any(per_inst.values()):
                    continue
                if nname in port_names_by_stem[stem]:
                    # Boundary net: it BELONGS to the parent (ir_schema.md
                    # "Net class": родительская цепь перезаписывает и имя,
                    # и класс) — the class rides up to the attached
                    # top-level net, never onto the module definition.
                    for inst, h in per_inst.items():
                        tnet = portref_net.get((inst, nname))
                        if tnet is None:
                            if h:
                                import_log.log(stem, f'{inst}:{nname}',
                                                'NETCLASS on an unconnected module port, dropped')
                            continue
                        top_hits.setdefault(tnet['name'], set()).update(h)
                else:
                    # Local module net: ONE class on the definition, shared
                    # by every instance — a per-instance difference is not
                    # expressible in IR (the net exists once, in the module
                    # definition), hard reject rather than silently picking.
                    distinct = {frozenset(h) for h in per_inst.values()}
                    if len(distinct) != 1:
                        nc_conflicts.append(
                            f'{stem}:{nname}: netclass differs between module instances '
                            f'({ {i: sorted(h) for i, h in per_inst.items()} })')
                        continue
                    h = next(iter(distinct))
                    if len(h) > 1:
                        nc_conflicts.append(f'{stem}:{nname}: multiple netclasses {sorted(h)}')
                        continue
                    mod_net_el = mod_el_by_stem[stem].find(f'net[@name="{nname}"]')
                    if mod_net_el is not None:
                        cname = next(iter(h))
                        mod_net_el.set('class', cname)
                        if wire_um_by_class.get(cname, wire_default_um) != wire_default_um:
                            # Module wires were already materialized at the
                            # Default width (_write_canvas ran before class
                            # assignment) — a class with its OWN wire_width
                            # would have rendered differently in KiCad.
                            import_log.log(stem, nname,
                                            f'NET_WIRE_WIDTH class "{cname}" wire_width not '
                                            f'applied inside module (materialized at Default '
                                            f'{wire_default_um}um)')

        for net in nets:
            h = top_hits.get(net['name'])
            if not h:
                continue
            if len(h) > 1:
                nc_conflicts.append(f'{net["name"]}: multiple netclasses {sorted(h)}')
            else:
                net['class'] = next(iter(h))

        if nc_conflicts:
            raise ValueError(
                f'{len(nc_conflicts)} net(s) resolve to more than one net class (or to '
                f'different classes across module instances) — IR has exactly one class '
                f'per net and no KiCad-style priority aggregation (ir_schema.md "Net '
                f'class"). Fix the netclass patterns and re-export:\n  ' +
                '\n  '.join(nc_conflicts))
        for p in sorted(unused_patterns):
            import_log.log('netclass_patterns', p, 'NETCLASS pattern matched no net, ignored')

    _write_canvas(schem_el, cv_top, nets, wire_default_um, wire_um_by_class)

    # --- board: .kicad_pcb -> <layout> (kicad_board_parser, raw s-expr).
    # A project with no board is a legitimate schematic-only project.
    if pcb_path.exists():
        if modules:
            # module boards carry flattened refdes (TM1:C1 -> C101) and
            # hierarchical net names — the reverse mapping isn't built yet
            # (flat projects first, simple -> complex)
            import_log.log('kicad_pcb', pro_path.stem, 'BOARD deferred',
                           f'hierarchical project ({len(modules)} module(s)) '
                           f'— module board import not supported yet')
        else:
            from babel.kicad_board_parser import convert_board
            known_refdes = {i.get('name') for i in schem_el.iter('instance')}
            convert_board(pcb_path, proj_el, known_refdes)

    raw = minidom.parseString(ET.tostring(proj_el, encoding='unicode')).toprettyxml(indent='  ')
    output_path.write_text(raw, encoding='utf-8')
    print(f'Written: {output_path}  ({len(proj_el.findall("component"))} components, '
          f'{len(modules)} modules, {len(cv_top.instances)} instances, {len(nets)} nets)')
    import_log.write(output_path)
    return str(output_path)


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'testData/phil'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'outputs/phil.swprj'
    convert_project_full(src, dst)
