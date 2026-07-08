"""KiCad .kicad_sym (+ .pretty via fp-lib-table) -> IR XML converter.

Library-only import (no .kicad_sch) — see progress.md "KiCad-парсер/импортёр"
for the atomic/generic split this follows (KiCad Library Conventions:
https://klc.kicad.org/general/g2/g2.1.html).
"""
import copy
import math
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

from kiutils.symbol import SymbolLib
from kiutils.footprint import Footprint
from kiutils.libraries import LibTable
from kiutils.items.common import Effects, Position
from kiutils.utils import sexpr as _kiutils_sexpr

from babel.ir_util import sanitize_filename, clean_attr_name
from babel import import_log

_MM_TO_UM = 1000

# KiCad pin electricalType -> IR direction (inverse of kicad_exporter._PIN_DIR).
_PIN_DIR = {
    'input':          'in',
    'output':         'out',
    'bidirectional':  'io',
    'power_in':       'pwr',
    'power_out':      'pwr',
    'passive':        'pas',
    'tri_state':      'out',
    'open_collector': 'out',
    'open_emitter':   'out',
    'unspecified':    'pas',
    'no_connect':     'nc',
    'free':           'pas',
}

def _is_erc_power_source(symbol):
    """True for a PWR_FLAG-style ERC directive: an `(power)` symbol whose
    pin is power_OUTPUT. KLC S7.1's supply-symbol shape (the one the
    isPower/`sup`-pin collapse applies to) is exactly one power_INPUT pin —
    Value names the net. A power symbol with a power_out pin is the other,
    structurally distinct KiCad idiom: it doesn't represent a rail, it
    DECLARES "this net has a driver" to silence ERC. Treating it as a
    supply made its Value ("PWR_FLAG") name the net and collide with the
    real net name on every project that uses the idiom (found on
    testData/Pocket-Lab-Bench-Power.zip: 5 nets hard-rejected by the
    conflicting-label check, all of them PWR_FLAG vs the real rail name).
    Such a symbol imports as an ORDINARY component — placed, rendered,
    connected, its pin never names anything.
    """
    return symbol.isPower and any(p.electricalType == 'power_out'
                                  for u in symbol.units for p in u.pins)


# Footprint type -> IR element tag.
_PAD_TAG = {'smd': 'smd', 'thru_hole': 'pad', 'np_thru_hole': 'hole', 'connect': 'smd'}

# Pad shape we can't represent exactly -> nearest IR shape (round/square only).
_PAD_SHAPE_FALLBACK = {
    'circle': 'round', 'oval': 'round', 'roundrect': 'square',
    'trapezoid': 'square', 'rect': 'square', 'custom': 'square',
}

# KiCad footprint layer name -> IR signed layer NUMBER (ir_schema.md "Плата
# (Board IR)"; ir_util.py has the canonical constants). Sign is side-relative
# in footprint space: + = mount side, - = far side.
_FP_LAYER_TO_IR = {
    'F.Cu':    1,    'B.Cu':    -1,
    'F.SilkS': 121,  'B.SilkS': -121,
    'F.Mask':  129,  'B.Mask':  -129,
    'F.Paste': 131,  'B.Paste': -131,
    'F.CrtYd': 139,  'B.CrtYd': -139,
    'F.Fab':   151,  'B.Fab':   -151,
    'Edge.Cuts': 120,
    # KiCad's generic (not top/bottom-specific) designer-notes layers — the
    # side-less Document layer (148) is the exact semantic match (they have
    # no side in KiCad either). Confirmed real content silently dropped
    # before this mapping existed (testData/kicad9-ti-mspm0-tutorial:
    # Tag-Connect footprint's "KEEPOUT" on Cmts.User, USB-C receptacle's
    # "PCB Edge" on Dwgs.User — found via Footprint Editor showing more
    # text than reached Eagle).
    'Dwgs.User': 148, 'Cmts.User': 148,
}


def _um(mm):
    return str(round(float(mm) * _MM_TO_UM))


def _f(v):
    r = round(float(v), 4)
    return str(int(r)) if r == int(r) else str(r)


def _rot(angle):
    """Normalize a KiCad rotation angle to [0, 360) before formatting.

    Some real-world libraries (often Altium-derived re-exports — see
    progress.md) store out-of-range angles like 900 instead of the reduced
    180; IR rotations elsewhere are always 0-360, so reduce defensively.
    """
    return _f((angle or 0) % 360)


def _circle_from_3pts(p1, p2, p3):
    """Center+radius of the circle through 3 points, or None if collinear."""
    (ax, ay), (bx, by), (cx, cy) = p1, p2, p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    return ux, uy, r


def _angle_deg(cx, cy, x, y):
    return math.degrees(math.atan2(y - cy, x - cx)) % 360


def _arc_params(p1, p2, p3):
    """3 points (start, mid, end) -> (cx, cy, r, start_deg, sweep_deg), or None.

    Inverse of kicad_exporter._sym_geom's arc branch: that emits start/mid/end
    points from (cx, cy, r, start, sweep) via standard math-angle parametrics
    (CCW from +X). sweep here is signed: positive = CCW, negative = CW —
    whichever direction actually passes through the mid point.
    """
    fit = _circle_from_3pts(p1, p2, p3)
    if fit is None:
        return None
    cx, cy, r = fit
    a1 = _angle_deg(cx, cy, *p1)
    am = _angle_deg(cx, cy, *p2)
    a3 = _angle_deg(cx, cy, *p3)
    sweep_ccw = (a3 - a1) % 360 or 360
    mid_offset = (am - a1) % 360
    sweep = sweep_ccw if mid_offset <= sweep_ccw + 1e-6 else sweep_ccw - 360
    return cx, cy, r, a1, sweep


def _contains_hide_yes(node):
    """Recursively true if `(hide yes)` appears anywhere inside node.

    Covers TWO distinct real-world placements found across different KiCad
    exports of the very same token, neither of which kiutils 1.4.8 parses:
    - sibling of `effects` directly under `<property>` (testData/multichannel,
      Altium-derived libraries): `(property "Library Ref" "..." (at ..)
      (show_name no) (do_not_autoplace no) (hide yes) (effects ...))`.
      `Property.from_sexpr` only looks at id/at/effects/show_name, ignores it.
    - nested INSIDE `effects` (testData/kicad9-ti-mspm0-tutorial, a plain
      modern KiCad 9 project — not Altium-derived, so this isn't an artifact
      of conversion): `(effects (font ...) (hide yes))`. `Effects.from_sexpr`
      only recognizes a BARE `hide` atom (`if item == 'hide'`), not this
      value-bearing `(hide yes)` list form, so it's skipped there too.
    Recursing instead of checking two fixed spots is deliberately more
    permissive — a property's own subtree has nothing else called `hide`,
    so there's no real ambiguity, and it survives a third placement showing
    up somewhere we haven't seen yet.
    """
    if isinstance(node, list):
        if node and node[0] == 'hide' and len(node) > 1 and node[1] == 'yes':
            return True
        return any(_contains_hide_yes(child) for child in node)
    return False


def _hidden_keys_from_symbol_nodes(symbol_nodes):
    """{(symbol_lib_id, property_key): True} for every <property> with a
    `(hide yes)` anywhere in it (see _contains_hide_yes), found among the
    given raw `(symbol ...)` S-expression nodes — workaround for the kiutils
    1.4.8 read gaps documented there. Confirmed against two different real
    projects (testData/multichannel, testData/kicad9-ti-mspm0-tutorial) —
    `prop.effects.hide` reads back False in both even though the source
    explicitly marks the property hidden. Related but not identical to
    https://github.com/mvnmgrx/kiutils/issues/120 (that one is about a
    schematic round-trip *write* loss; this is a *read* gap).

    Takes raw nodes rather than a whole file's tree because the same gap
    bites two different callers with two different node locations: a plain
    `.kicad_sym` lib has `(symbol ...)` directly under the root, while a
    `.kicad_sch`'s cached `lib_symbols` block nests them one level deeper —
    see kicad_project_parser.py's own variant of this function.
    """
    hidden = {}
    for item in symbol_nodes:
        if not (isinstance(item, list) and item and item[0] == 'symbol'):
            continue
        sym_name = item[1]
        for sub in item[2:]:
            if isinstance(sub, list) and sub and sub[0] == 'property' and _contains_hide_yes(sub):
                hidden[(sym_name, sub[1])] = True
    return hidden


def _parse_hidden_property_keys(text):
    """Same as _hidden_keys_from_symbol_nodes, for a plain `.kicad_sym`
    library file (`(symbol ...)` nodes directly under the root)."""
    tree = _kiutils_sexpr.parse_sexp(text)
    return _hidden_keys_from_symbol_nodes(tree[1:])


def _pin_label_globals_from_symbol_nodes(symbol_nodes):
    """{symbol_lib_id: (names_hidden, numbers_hidden)} — same kiutils gap as
    _hidden_keys_from_symbol_nodes, but for the symbol-wide `pin_numbers`/
    `pin_names` hide toggle: kiutils' Symbol.from_sexpr only recognizes a
    bare `hide` atom (`(pin_numbers hide)`), via a literal `item[1] ==
    'hide'` check — not the nested value-list form `(pin_numbers (hide
    yes))`, where `item[1]` is the list `['hide', 'yes']`, never equal to
    the string `'hide'`. Confirmed on a real, NOT Altium-derived KiCad 9
    project (testData/kicad9-ti-mspm0-tutorial, Device:C — user found it by
    comparing the real Symbol Editor's "Show Pin Number" checkbox (off)
    against our generated IR (padvis="1", wrong) — the file has exactly
    `(pin_numbers (hide yes))`, kiutils reads hidePinNumbers=False).
    `pin_names`'s own bare-atom-sibling form IS handled correctly by kiutils
    already (`else: if property == 'hide'`); this only plugs the same
    nested-list gap for whichever of the two tokens hits it.
    """
    out = {}
    for item in symbol_nodes:
        if not (isinstance(item, list) and item and item[0] == 'symbol'):
            continue
        sym_name = item[1]
        names_hidden = numbers_hidden = False
        for sub in item[2:]:
            if isinstance(sub, list) and sub and sub[0] == 'pin_numbers' and _contains_hide_yes(sub):
                numbers_hidden = True
            if isinstance(sub, list) and sub and sub[0] == 'pin_names' and _contains_hide_yes(sub):
                names_hidden = True
        out[sym_name] = (names_hidden, numbers_hidden)
    return out


def _pin_hide_flags_from_symbol_nodes(symbol_nodes):
    """{unit_raw_name: [bool, ...]} — per-pin `(hide yes)` flags, in raw file
    order (matches `unit.pins`' own order, since both walk the same pin
    list) — same kiutils 1.4.8 gap as `_pin_label_globals_from_symbol_nodes`/
    `_contains_hide_yes`, but for the WHOLE PIN's own `(pin ... (hide yes)
    ...)` token, not just its name/number sub-label: `SymbolPin.from_sexpr`
    only recognizes a bare `hide` atom (`item == 'hide'`), never the real,
    value-bearing `(hide yes)` list KiCad actually writes — confirmed
    `pin.hide` reads back False unconditionally regardless of source
    (testData/phil/phil.kicad_sym, `USB_C_Receptacle_USB2.0_14P`'s shield/
    GND/VBUS pins — real `(hide yes)` in the file, Symbol Editor's own "Show
    pin" checkbox unticked, kiutils silently drops it).

    `unit_raw_name` is each nested `(symbol "Entry_unitId_styleId" ...)`
    sub-block's own name token — exactly the string kiutils reconstructs as
    `f'{unit.entryName}_{unit.unitId}_{unit.styleId}'` (confirmed identical
    for every unit in a real multi-unit symbol), so the caller can match
    `unit.pins` positionally without re-deriving any kiutils internals.
    """
    out = {}

    def pin_hidden(pin_node):
        # Direct children only — NOT recursive (unlike _contains_hide_yes):
        # a `(hide yes)` nested inside this pin's own `name`/`number`
        # sub-list means only THAT label is hidden (a different, already-
        # handled per-label case, _pin_label_hidden), not the whole pin.
        return any(isinstance(x, list) and x and x[0] == 'hide' and len(x) > 1 and x[1] == 'yes'
                   for x in pin_node[3:])

    def walk(node):
        if isinstance(node, list) and node and node[0] == 'symbol' and len(node) > 1:
            pins = [p for p in node[2:] if isinstance(p, list) and p and p[0] == 'pin']
            if pins:
                out[node[1]] = [pin_hidden(p) for p in pins]
        if isinstance(node, list):
            for child in node:
                walk(child)

    for item in symbol_nodes:
        walk(item)
    return out


def _collapse_stacked_pins(symbol):
    """Collapse KiCad's "one logical pin, several footprint pads" hack into
    one representative <pin> per coordinate — see decisions.md "Stacked
    pins" for the full story (the user's own explanation, confirmed against
    testData/pintest.kicad_sym): KiCad has no way to map one symbol pin to
    several pads directly, so older libraries stack several <pin> elements
    on the EXACT SAME coordinate and hide all but one.

    Per the user: coordinate match alone is the grouping signal — `hide` is
    NOT used to decide the grouping (it's also unreliable to read at all,
    see the kiutils gap below) — KiCad has no other reason to put two pins
    on the exact same spot. Mutates unit.pins in place, once, right after
    parsing — every downstream consumer (_named_pins, _add_unit_geometry,
    _pin_mapping via _pin_numbers) only ever sees the collapsed shape.

    The representative is just the first pin encountered per coordinate
    (same "take the first, don't guess which is more canonical" convention
    used elsewhere in this project) — survives fine even though `pin.hide`
    itself can't be trusted: kiutils' `if item == 'hide'` only recognizes a
    bare atom, not the `(hide yes)` value-bearing list form this library
    actually uses (same class of gap as Property/pin_numbers hide, just a
    third token hitting it) — confirmed every duplicate reads `hide=False`
    regardless of the source.

    A KiCad 10 "stacked pin" (one real <pin>, `number` itself a bracketed
    list like `"[A1,A12,B1,B12]"`) is a no-op here — group size 1, nothing
    to collapse; _pin_numbers() expands the bracket string later.
    """
    for unit in symbol.units:
        groups = {}
        order = []
        for pin in unit.pins:
            key = (pin.position.X, pin.position.Y)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(pin)
        collapsed = []
        for key in order:
            group = groups[key]
            rep = group[0]
            rep._stacked_numbers = [p.number for p in group]
            collapsed.append(rep)
        unit.pins = collapsed


def _read_symbol_lib(path):
    text = Path(path).read_text(encoding='utf-8')
    lib = SymbolLib.from_sexpr(_kiutils_sexpr.parse_sexp(text))
    hidden = _parse_hidden_property_keys(text)
    tree = _kiutils_sexpr.parse_sexp(text)
    pin_label_globals = _pin_label_globals_from_symbol_nodes(tree[1:])
    pin_hide_flags = _pin_hide_flags_from_symbol_nodes(tree[1:])
    for symbol in lib.symbols:
        for prop in symbol.properties:
            if (symbol.libId, prop.key) in hidden:
                if prop.effects is None:
                    prop.effects = Effects()
                prop.effects.hide = True
        names_hidden, numbers_hidden = pin_label_globals.get(symbol.libId, (False, False))
        symbol.pinNamesHide = symbol.pinNamesHide or names_hidden
        symbol.hidePinNumbers = symbol.hidePinNumbers or numbers_hidden
        for unit in symbol.units:
            raw_name = f'{unit.entryName}_{unit.unitId}_{unit.styleId}'
            flags = pin_hide_flags.get(raw_name)
            if flags and len(flags) == len(unit.pins):
                for pin, is_hidden in zip(unit.pins, flags):
                    pin.hide = pin.hide or is_hidden
        _collapse_stacked_pins(symbol)
    return lib


def _footprint_name_value_geometry(text):
    """{'Reference'|'Value': (Position, Effects|None, layer, hide)} for a
    footprint's own `(property "Reference"/"Value" "..." (at ..) (layer ..)
    [hide yes] (effects ..))` tokens.

    kiutils.Footprint.from_sexpr only keeps the bare value
    (`object.properties.update({item[1]: item[2]})`), discarding position/
    layer/hide/effects entirely — same class of gap as the symbol-side
    Property.hide ones already worked around this session, confirmed on a
    real modern-format footprint (testData/phil.zip, SOT-23-5: "Value"
    property sits at (0, 2.4) on F.Fab, visible — currently silently
    dropped, while a same-footprint decorative `fp_text user "${REFERENCE}"`
    with no real position info is the only thing that reaches IR).

    Per the user: only Reference/Value matter here, and only their GEOMETRY
    (position, text size, justify) — never their actual content (`REF**`,
    a part number, ...), which becomes the IR >NAME/>VALUE placeholder
    exactly like the symbol side, not real text. Datasheet/Description/
    custom fields on a footprint are out of scope (deliberately — they
    duplicate the symbol's own, confirmed identical on SOT-23-5).
    """
    tree = _kiutils_sexpr.parse_sexp(text)
    if not tree or tree[0] != 'footprint':
        return {}
    out = {}
    for item in tree[1:]:
        if not (isinstance(item, list) and item and item[0] == 'property'):
            continue
        if len(item) < 2 or item[1] not in ('Reference', 'Value'):
            continue
        at_node = next((s for s in item[2:] if isinstance(s, list) and s and s[0] == 'at'), None)
        layer = next((s[1] for s in item[2:] if isinstance(s, list) and s and s[0] == 'layer'), 'F.Fab')
        effects_node = next((s for s in item[2:] if isinstance(s, list) and s and s[0] == 'effects'), None)
        out[item[1]] = (
            Position.from_sexpr(at_node) if at_node else Position(),
            Effects.from_sexpr(effects_node) if effects_node else None,
            layer,
            _contains_hide_yes(item),
        )
    return out


def _read_footprint(path):
    fp = Footprint.from_file(str(path), encoding='utf-8')
    fp._name_value_geometry = _footprint_name_value_geometry(Path(path).read_text(encoding='utf-8'))
    return fp


def _align(justify):
    """kiutils Justify -> IR align string ('{vertical}-{horizontal}', or
    plain 'center' when both are centered — matches the convention used
    throughout this project, e.g. altium_parser._JUSTIFICATION).

    In KiCad, an absent horizontally/vertically token means CENTERED on that
    axis, not left/bottom — `justify.horizontally`/`.vertically` are None in
    that case. Defaulting None to 'left'/'bottom' (the bug this replaces)
    silently turns every centered property into bottom-left, since real
    libraries very commonly leave one or both axes unset.
    """
    if justify is None:
        return 'center'
    v = {'top': 'top', 'bottom': 'bottom'}.get(justify.vertically, 'center')
    h = {'left': 'left', 'right': 'right'}.get(justify.horizontally, 'center')
    if v == 'center' and h == 'center':
        return 'center'
    return f'{v}-{h}'


def _is_hidden(prop):
    """True if a kiutils Property's effects mark it hidden (no .effects -> visible)."""
    return bool(prop.effects and prop.effects.hide)


# KiCad-internal Property keys (dev-docs.kicad.org "Reserved Symbol Property
# Keys") — system-managed metadata, not something a user fills in through
# any visible Symbol Properties dialog (ki_fp_filters is already special-
# cased elsewhere for its OWN purpose — generic-component detection — this
# is the placeholder-visibility side, separate concern). Confirmed real:
# testData/multigate's 74AHC04 (official KiCad library symbol) carries
# `ki_locked=""` with NO `(hide yes)` — the field simply isn't reachable
# through KiCad's own UI to fix at the source, so a visible `>KI_LOCKED`
# placeholder on the symbol body is pure clutter, never something the
# user meant to show. Per the user: still collected as a normal `<attr>`
# (round-trip value preserved, ir_schema.md "Field-парсинг: собирать ВСЕ"
# still applies to the ATTRIBUTE), just never gets a placeholder text on
# the symbol body regardless of its own hide flag — see ir_schema.md
# "KiCad: зарезервированные ki_*-поля — атрибут да, плейсхолдер никогда".
_KICAD_RESERVED_PROP_KEYS = {'ki_locked', 'ki_keywords', 'ki_description', 'ki_fp_filters'}


def _text_size_um(effects):
    """Effects.font.height (mm) -> IR size, µm. Falls back to KiCad's 1.27mm default."""
    if effects is None or effects.font is None or effects.font.height is None:
        return _um(1.27)
    return _um(effects.font.height)


def _kicad_blank(s):
    """True if s is KiCad's way of saying "no value" — either truly empty,
    or the literal sentinel `~` (legacy convention: older KiCad versions
    didn't allow an empty string for a pin name, so `~` stood in for "no
    name"; the same file can ALSO use a genuinely empty string for the same
    "no value" meaning elsewhere — confirmed on testData/kicad9-ti-mspm0-
    tutorial, both forms coexist: 7 properties literally `~`, 11 literally
    "" — so neither alone is a reliable "is this blank" check on its own).
    Eagle/Altium have no concept of `~` as a placeholder, so anywhere we'd
    otherwise use the raw value (pin name fallback, attribute value), both
    forms must be treated identically as "blank".
    """
    return not s or s == '~'


_OVERBAR_RE = re.compile(r'~\{([^}]*)\}')


def _kicad_overbar_to_eagle(s):
    """KiCad's `~{TEXT}` overbar notation (horizontal line over TEXT, used
    for active-low signal names like ~{RESET}) -> Eagle's `!TEXT!` toggle
    convention, which IR adopts as its own canon — per the user, this
    applies everywhere a raw string from KiCad can end up as visible text:
    pin names, pad names/numbers, and plain symbol/footprint text.

    KiCad delimits the overlined span explicitly with braces, so each group
    converts independently into its own toggle-on + toggle-off pair —
    `~\\{([^}]*)\\}` never matches the bare `~` "no value" sentinel (see
    _kicad_blank), since that has no `{` immediately after it.
    """
    if not s:
        return s
    return _OVERBAR_RE.sub(lambda m: f'!{m.group(1)}!', s)


def _kicad_multiline_to_eagle(s):
    """KiCad graphic `(text ...)` items store a line break as the literal
    two-character escape `\\n` (backslash + 'n'), NOT a real newline byte —
    confirmed on real data (testData/phil, symlib.kicad_sym: `repr()` of the
    raw file bytes around the "TEXAS\\nINSTRUMENTS..." string shows a
    literal `\\\\n`, not `\\n`/0x0A). Eagle's own convention is the opposite:
    a real embedded newline character, confirmed by the user's own ground-
    truth test in real Eagle (manually duplicating the same text with an
    actual line break instead of the escape — only that version rendered
    multi-line; the literal-escape version showed the literal text "\\n").
    IR canon follows Eagle here, like the overbar convention above, so this
    converts at the KiCad import boundary only.
    """
    if not s:
        return s
    return s.replace('\\n', '\n')


def _pin_label_hidden(effects):
    """True if a pin's Name/Number text size is explicitly set to 0 — per the
    user, that's how KiCad lets you hide just the name OR just the number on
    one specific pin (Pin Properties dialog: "Name text size"/"Number text
    size" == 0 -> hidden), independent of the symbol-wide pinNamesHide/
    hidePinNumbers toggle and of the pin's own overall `hide` flag.

    Missing effects (kiutils' SymbolPin.nameEffects/numberEffects, optional
    since KiCad v7) means "no override", NOT hidden — only an explicit
    font height of 0 counts.
    """
    return effects is not None and effects.font is not None and effects.font.height == 0


_SYM_DEFAULT_STROKE_UM = '152'   # 6 mil = 6 * 25.4 µm — see _stroke_width_um

# Coordinate rounding in source files (4 decimal places) means a "true"
# semicircle's fitted sweep comes back as e.g. 179.51deg, not exactly 180 —
# see the filled-arc-to-circle branch in _add_unit_geometry.
_ARC_SEMICIRCLE_TOL_DEG = 1.0

# KiCad footprint-graphics `(fill ...)` token -> IR <polygon> fill percent
# (ir_schema.md "<polygon>": 100=solid, 0=outline only, in-between=hatch
# density). Confirmed against real files (testData/multigate/Library.pretty/
# SOIC-20W.kicad_mod) — the token is a FLAT string (`(fill cross_hatch)`),
# not the nested `(fill (type ...))` form kiutils' own `Fill` class parses
# (that class is for SYMBOL graphics, a different token shape — FpPoly/
# FpRect/FpCircle store `.fill` as a plain Optional[str] instead, already
# read correctly by kiutils as-is). `yes`/`no` are the legacy pre-hatch
# boolean spelling (confirmed real: `(fill yes)` on the same file, older
# KiCad style), still emitted for plain Solid/None even in current KiCad —
# not a stale/pre-v7 artifact to special-case away. `hatch`/`reverse_hatch`
# collapse to the SAME percent — same physical copper density, only the
# visual hatch-vs-gap line assignment is swapped, no percent difference.
_FP_FILL_PERCENT = {
    'no': 0, 'none': 0,
    'hatch': 15, 'reverse_hatch': 15,
    'cross_hatch': 30,
    'yes': 100, 'solid': 100,
}


def _stroke_width_um(item, default_um='0'):
    """Item's stroke.width (mm, KiCad7+) -> IR width, µm. 0/missing -> default_um.

    For schematic symbols, pass default_um=_SYM_DEFAULT_STROKE_UM, not '0':
    KiCad itself doesn't treat a symbol stroke width of 0 as "invisible" —
    confirmed against KiCad's own bug tracker (maintainer reply on
    https://gitlab.com/kicad/code/kicad/-/issues/9776): "the polygon is
    always drawn with a default 6 mil thickness" whenever width is 0,
    hardcoded, not user-configurable. A literal 0-width stroke would be
    invisible and pointless on a schematic, so 0 means "unset, use the
    editor's real rendered default", not "thin". Footprint-side callers keep
    the plain '0' default — not (yet) confirmed to behave the same way for
    PCB graphics.
    """
    w = getattr(getattr(item, 'stroke', None), 'width', None)
    if not w:
        w = getattr(item, 'width', None)
    return _um(w) if w else default_um


# ---------------------------------------------------------------------------
# Symbol unit geometry -> IR
# ---------------------------------------------------------------------------

def _named_pins(units):
    """Dedup pin names with an '@N' suffix across one or more merged unit
    bodies (a gate = its own unit + the shared unit-0 body, see _group_gates)
    — same convention as altium_parser._iter_named_pins, and for the same
    reason: a chip with several pins literally named "NC" needs unique IR
    pin names, and the symbol drawing and the (future) pin-mapping must agree
    on them, so this is computed once and reused.

    Yields (ir_name, pin) in stable order.

    A HIDDEN no_connect pin is skipped here, not just left undrawn by
    _add_unit_geometry — this is the ONE list _add_unit_geometry, _pin_mapping
    AND kicad_project_parser.py's positional `zip(named_pins, sym_el.findall
    ('pin'))` all consume, so filtering anywhere else leaves the others
    referencing a <pin> that was never actually drawn (confirmed real: a
    stray `<connect pin="NC">` with no matching `<pin name="NC">` in the
    same gate — real Eagle rejected the file outright, "invalid/missing
    attribute 'pin' in tag <connect>"). See ir_schema.md/decisions.md
    "IR `direction=\"nc\"`" for why a VISIBLE no_connect pin is kept instead.
    """
    seen = {}
    for unit in units:
        for pin in unit.pins:
            if pin.electricalType == 'no_connect' and pin.hide:
                continue
            name = pin.number if _kicad_blank(pin.name) else clean_attr_name(_kicad_overbar_to_eagle(pin.name))
            seen[name] = seen.get(name, 0) + 1
            n = seen[name]
            yield (name if n == 1 else f'{name}@{n}', pin)


def _add_unit_geometry(sym_el, unit, named_pins, names_hidden, numbers_hidden, *, footprint_space=False):
    """Append one kiutils Symbol unit's graphics as IR <symbol> children, plus
    pins from named_pins (an [(ir_name, pin), ...] list — see _named_pins;
    not necessarily unit.pins itself, since a gate's pins may be merged in
    from a shared unit-0 body too).

    names_hidden/numbers_hidden are the *symbol*-level (not per-unit)
    pinNamesHide/hidePinNumbers globals — KiCad applies them to every pin
    unless the individual pin is itself hidden too.

    footprint_space=False (always, for the symbol side): KiCad symbol
    coordinates are Y-up, same as IR — no flip (mirrors kicad_exporter's
    _sym_geom, which doesn't apply _ky either). The parameter exists only so
    a future schematic-side reuse of this function can't silently assume the
    wrong convention.
    """
    assert not footprint_space, "symbol-side geometry is Y-up, no flip"

    for item in unit.graphicItems:
        tag = type(item).__name__

        # KiCad has no layer concept at all in a schematic/symbol (ir_
        # schema.md "Слои" is our own, Eagle-modeled convention) — every
        # body-geometry primitive below gets layer='SYMBOLS' by hand, same
        # manual choice made for SyText further down, so the whole symbol
        # body is consistently marked instead of half explicit/half absent.
        if tag == 'SyRect':
            if item.fill.type in ('outline', 'color'):
                # Filled with the BODY OUTLINE COLOR specifically (KiCad's
                # `fill type outline`) -> <shape outline="0"> (fills with
                # the layer's own color, no separate color data needed —
                # see decisions.md "KiCad-специфика: filled-графика").
                # `fill type background` is NOT this — it fills with
                # whatever the EDITOR'S CANVAS background happens to be,
                # not a meaningful "solid" color at all (per the user: this
                # exact mistake turned the connector body's outline rect
                # into one giant solid block covering everything drawn
                # under it). Anything other than 'outline' keeps the old
                # unfilled-lines behavior, same as 'none'.
                ET.SubElement(sym_el, 'shape',
                              x=_um((item.start.X + item.end.X) / 2),
                              y=_um((item.start.Y + item.end.Y) / 2),
                              w=_um(abs(item.end.X - item.start.X)),
                              h=_um(abs(item.end.Y - item.start.Y)),
                              roundness='0', outline='0', rot='0', layer='SYMBOLS')
            else:
                x1, y1 = _um(item.start.X), _um(item.start.Y)
                x2, y2 = _um(item.end.X), _um(item.end.Y)
                w = _stroke_width_um(item, _SYM_DEFAULT_STROKE_UM)
                for ax1, ay1, ax2, ay2 in [(x1, y1, x2, y1), (x2, y1, x2, y2),
                                            (x2, y2, x1, y2), (x1, y2, x1, y1)]:
                    ET.SubElement(sym_el, 'line', x1=ax1, y1=ay1, x2=ax2, y2=ay2,
                                  width=w, layer='SYMBOLS')

        elif tag == 'SyPolyLine':
            # One KiCad token for both "line" and "polygon": a segment is a
            # 2-point polyline, a polygon is an N-point one — closedness is
            # only the last point coinciding with the first, and KiCad
            # renders a fill even on an OPEN polyline (fills the implicitly
            # closed area, strokes only the drawn edges). So: a polyline is
            # a polygon when it's closed OR filled; only unfilled open
            # chains decompose into <line>s. 'outline'/'color' fill solid
            # (100%); 'none'/'background' keep an unfilled contour (0%) —
            # 'background' is the editor-canvas-color trap, not a
            # meaningful solid fill, same reasoning as the SyRect/SyCircle/
            # SyArc branches.
            pts = item.points
            closed = len(pts) > 2 and pts[0].X == pts[-1].X and pts[0].Y == pts[-1].Y
            fill_solid = item.fill.type in ('outline', 'color')
            w = _stroke_width_um(item, _SYM_DEFAULT_STROKE_UM)
            if closed or (fill_solid and len(pts) > 2):
                poly = ET.SubElement(sym_el, 'polygon', width=w,
                                      fill=str(100 if fill_solid else 0), layer='SYMBOLS')
                for p in (pts[:-1] if closed else pts):
                    ET.SubElement(poly, 'vertex', x=_um(p.X), y=_um(p.Y))
            else:
                for a, b in zip(pts, pts[1:]):
                    ET.SubElement(sym_el, 'line',
                                  x1=_um(a.X), y1=_um(a.Y), x2=_um(b.X), y2=_um(b.Y),
                                  width=w, layer='SYMBOLS')

        elif tag == 'SyCircle':
            if item.fill.type in ('outline', 'color'):
                ET.SubElement(sym_el, 'shape',
                              x=_um(item.center.X), y=_um(item.center.Y),
                              w=_um(item.radius * 2), h=_um(item.radius * 2),
                              roundness='100', outline='0', rot='0', layer='SYMBOLS')
            else:
                ET.SubElement(sym_el, 'arc',
                              cx=_um(item.center.X), cy=_um(item.center.Y), r=_um(item.radius),
                              start='0', sweep='360',
                              width=_stroke_width_um(item, _SYM_DEFAULT_STROKE_UM), layer='SYMBOLS')

        elif tag == 'SyArc':
            params = _arc_params((item.start.X, item.start.Y),
                                  (item.mid.X, item.mid.Y),
                                  (item.end.X, item.end.Y))
            if params is None:
                continue
            cx, cy, r, start, sweep = params
            if item.fill.type in ('outline', 'color') and abs(abs(sweep) - 180) < _ARC_SEMICIRCLE_TOL_DEG:
                # Filled near-semicircle -> filled circle of the same
                # radius/center. Exact identity, not an approximation — see
                # decisions.md "KiCad-специфика: filled дуга → круг": a
                # filled ~180° arc in a KiCad symbol is always the rounded
                # end-cap of an equally-filled rect of matching width (no
                # native "rounded rect" primitive for symbols), so the
                # circle's other half just lands inside that rect's own
                # fill — nothing extra becomes visible. Gated strictly on
                # sweep, not "any filled arc": a genuinely different sweep
                # (a filled pie wedge, say) would visibly gain area if
                # doubled into a full circle, so it keeps the old unfilled
                # behavior below instead.
                ET.SubElement(sym_el, 'shape', x=_um(cx), y=_um(cy),
                              w=_um(r * 2), h=_um(r * 2),
                              roundness='100', outline='0', rot='0', layer='SYMBOLS')
            else:
                # cx/cy/r come from _arc_params in mm (raw kiutils units) —
                # _um() here, not _f(): ir_schema.md's <arc> wants µm like
                # everywhere else. Previously these went through _f() bare,
                # storing mm values mislabeled as µm (1000x too small) for
                # every non-full-circle KiCad arc — found while touching
                # this exact line, unrelated to the fill fix above.
                ET.SubElement(sym_el, 'arc', cx=_um(cx), cy=_um(cy), r=_um(r),
                              start=_f(start), sweep=_f(sweep),
                              width=_stroke_width_um(item, _SYM_DEFAULT_STROKE_UM), layer='SYMBOLS')

        elif tag == 'SyText':
            if _is_hidden(item):
                continue   # invisible decorative text — nothing to draw, don't carry it into IR
            # Free-standing body text, not a >NAME/>VALUE/>ATTR placeholder
            # (see _emit_property_placeholder for those) — same manual
            # SYMBOLS choice as the geometry above.
            t = ET.SubElement(sym_el, 'text',
                               x=_um(item.position.X), y=_um(item.position.Y),
                               size=_text_size_um(item.effects),
                               rot=_rot(item.position.angle),
                               align=_align(item.effects.justify if item.effects else None),
                               layer='SYMBOLS')
            t.text = _kicad_overbar_to_eagle(_kicad_multiline_to_eagle(item.text))

    unit_pin_ids = {id(p) for p in unit.pins}
    for ir_name, pin in named_pins:
        if id(pin) not in unit_pin_ids:
            continue
        # Hidden no_connect pins are already filtered out of named_pins
        # itself (_named_pins) — must stay that way, not re-checked here:
        # this list is also what _pin_mapping and kicad_project_parser.py's
        # positional pin zip consume, and filtering only at THIS one
        # drawing step left those other two still referencing a <pin> that
        # was never actually created (confirmed real: a dangling
        # `<connect pin="NC">` with no matching <pin>, real Eagle rejected
        # the file outright). See ir_schema.md/decisions.md "IR
        # `direction=\"nc\"`" for why a VISIBLE no_connect pin is kept.
        direction = _PIN_DIR.get(pin.electricalType, 'pas')
        # A blank pin.name (per the user — including KiCad's "~" sentinel,
        # see _kicad_blank) means there's no real name to show at all: the
        # IR name above already fell back to the pad number in that case
        # (_named_pins), so showing it as a "name" label too would just
        # duplicate the number under a different hat. Force pinvis off
        # regardless of whatever the symbol/pin-level visibility says.
        name_hidden = (names_hidden or pin.hide or _pin_label_hidden(pin.nameEffects)
                       or _kicad_blank(pin.name))
        num_hidden = numbers_hidden or pin.hide or _pin_label_hidden(pin.numberEffects)
        ET.SubElement(sym_el, 'pin',
                      name=ir_name, direction=direction,
                      x=_um(pin.position.X), y=_um(pin.position.Y),
                      rot=_rot(pin.position.angle), length=_um(pin.length),
                      pinvis='0' if name_hidden else '1',
                      padvis='0' if num_hidden else '1')


def _group_gates(symbol):
    """Group a Symbol's units into [(gate_letter_or_None, [bodies_to_merge]), ...].

    See progress.md "KiCad-парсер/импортёр" for the unitId=0 rule, confirmed
    against real KiCad behavior: TL072CD opens with exactly 2 units in the
    Symbol Editor's unit dropdown (A/B), not 3 — even though unitId 0/1/2 all
    carry real pins. unitId=0 is KiCad's "common to all units" slot, never a
    separately selectable gate; its pins+graphics merge into every unit with
    unitId>=1. A symbol with only ONE real unit besides unit 0 (e.g. GND,
    +12V — unit 0 holds shared decorative graphics, unit 1 the only pin) is
    not actually multi-gate, just single-mode with two bodies to merge.

    styleId mirrors unitId's own convention one level down: styleId 0 is
    COMMON TO ALL BODY STYLES (drawn in every view), styleId 1 is the
    standard body, styleId >= 2 are De Morgan alternates (dropped — same
    "pick one, don't model alternates" rule used for ellipses elsewhere in
    this project). A unit's rendered standard view is therefore the SUM of
    its styleId-0 and styleId-1 bodies, never a choice between them — an
    earlier version picked one (prefer 1, else 0), which silently dropped
    everything in the other slot; found by the user on a real symbol
    (Pocket-Lab-Bench-Power PL:TVS2200DRV — unit 0 carries the "Det & Drv"
    box, its wiring and both texts in _0_0 AND the MOSFET graphics in
    _0_1). Summing also subsumes the two legacy-split special cases the
    picker had grown: pins hiding in styleId 0 under a real unit
    (testData/phil MSPM0G3507SPTR, unit 1: NRST/debug pins in _1_0, empty
    _1_1) and the everything-under-unit-0 shape (testData/video power:VCC —
    pin in _0_0, graphics in _0_1).
    """
    by_unit = {}
    for u in symbol.units:
        by_unit.setdefault(u.unitId, {})[u.styleId] = u

    def _unit_bodies(styles):
        bodies = [styles[s] for s in (0, 1) if s in styles]
        # Degenerate file with only De Morgan slots — take the lowest so at
        # least one body survives rather than none.
        return bodies or [styles[min(styles)]]

    common = by_unit.get(0)
    common_bodies = _unit_bodies(common) if common else []
    real_unit_ids = sorted(u for u in by_unit if u != 0)

    if len(real_unit_ids) <= 1:
        bodies = _unit_bodies(by_unit[real_unit_ids[0]]) if real_unit_ids else []
        return [(None, bodies + common_bodies)]

    gates = []
    for i, uid in enumerate(real_unit_ids):
        gates.append((chr(ord('A') + i), _unit_bodies(by_unit[uid]) + common_bodies))
    return gates


def _unit_id_to_gate_letter(symbol):
    """KiCad `unitId` (as seen on a PLACED SchematicSymbol's own `.unit`
    field) -> the same gate letter `_group_gates` assigned that unit when
    building the library-side pool (ir_schema.md "Размещение многорежимного
    компонента"). Kept as a SEPARATE lookup, not threaded out of
    `_group_gates` itself, so the two can't silently drift if one changes —
    this reproduces only the letter-assignment half (sorted real unit ids,
    A/B/C/... by position), not the body-merging logic, since a placed
    instance only ever needs to know WHICH gate it is, never its geometry.

    Single-mode symbols (<=1 real unit besides unit 0) have no letters at
    all (`_group_gates` returns `[(None, bodies)]`) — this returns an empty
    dict, and callers should treat that as "no gate concept applies".
    """
    real_unit_ids = sorted({u.unitId for u in symbol.units if u.unitId != 0})
    if len(real_unit_ids) <= 1:
        return {}
    return {uid: chr(ord('A') + i) for i, uid in enumerate(real_unit_ids)}


def _build_gate_symbol(sym_name, bodies, names_hidden, numbers_hidden):
    """One IR <symbol> for a gate (or the whole symbol, if single-mode) —
    geometry+pins from all of `bodies` merged together (see _group_gates).
    Returns (sym_el, named_pins) — named_pins is reused for pin-mapping.
    """
    sym_el = ET.Element('symbol', name=sym_name)
    named_pins = list(_named_pins(bodies))
    for body in bodies:
        _add_unit_geometry(sym_el, body, named_pins, names_hidden, numbers_hidden)
    return sym_el, named_pins


_PLACEHOLDER_LAYER = {'NAME': 'NAMES', 'VALUE': 'VALUES'}


def _emit_property_placeholder(sym_el, placeholder, prop):
    """Add a >PLACEHOLDER text to a gate's symbol body at a property's real
    position/style — the inverse of kicad_exporter's placeholder_style
    extraction. Eagle/Altium have no separate "property" concept; they derive
    Reference/Value/custom-attr position purely from this placeholder text,
    so without it the position is lost on a round-trip through those formats.

    Layer routing matches eagle_exporter._SYM_TEXT_LAYER (Eagle's real
    schematic text layers): >NAME -> NAMES (95), >VALUE -> VALUES (96),
    every other placeholder (custom attrs) -> INFO (97). Eagle's own
    content-based fallback (_text_layer) only special-cases NAME/VALUE and
    dumps everything else on the plain SYMBOLS layer (94) — so a custom
    attr placeholder needs this set explicitly or it silently lands on the
    wrong layer on export.
    """
    effects = prop.effects
    justify = effects.justify if effects else None
    t = ET.SubElement(sym_el, 'text',
                       x=_um(prop.position.X), y=_um(prop.position.Y),
                       size=_text_size_um(effects),
                       rot=_rot(prop.position.angle),
                       align=_align(justify),
                       layer=_PLACEHOLDER_LAYER.get(placeholder, 'INFO'))
    t.text = f'>{placeholder}'


# ---------------------------------------------------------------------------
# Symbol -> pool + component info
# ---------------------------------------------------------------------------

def _convert_symbol(symbol, pool, symbols_el):
    """Register symbol's gate(s) into the pool; return component info for the
    caller to build a <component> from.

    Power-flag symbols (#PWR, e.g. GND/+3V3) DO get a <component> — no
    footprint (KiCad: Footprint property always blank for these), single
    `sup` pin instead of the usual ERC type. See the isPower branch below.

    Field handling rewritten per the user's explicit rules (see
    kicad_project_parser._convert_schematic_symbol for the fuller rationale,
    this mirrors it): every Field becomes an `<attr>` with its real value
    regardless of visibility, no exceptions besides Reference (names/
    prefixes the device, not an attribute). The symbol body only gets a
    >PLACEHOLDER for a Field that's actually visible. `footprint_ref`/
    `is_generic` below are a SEPARATE concern (library-only footprint
    resolution via fp-lib-table, decides whether this symbol gets a
    <component> at all) — unrelated to the Field/attr rules, untouched.
    """
    if not symbol.units:
        # KiCad `extends` (derived/alias symbol, e.g. a connector variant
        # that inherits another symbol's whole body) — confirmed real
        # (testData/video, "BUSPCI-5V" extends "PCI_CONUNIV": zero units of
        # its own). kiutils doesn't resolve `extends`, so there's nothing to
        # draw — skip rather than crash in _group_gates. Proper `extends`
        # support (look up & reuse the base symbol's units) is a separate,
        # not-yet-done feature.
        print(f'  ! {symbol.libId}: derived symbol (extends another symbol) '
              f'— not yet supported, skipped.')
        return None

    props = {p.key: p for p in symbol.properties}
    ref_prop = props.get('Reference')
    prefix = (ref_prop.value.rstrip('?') if ref_prop and ref_prop.value else '') or 'U'
    fp_prop = props.get('Footprint')
    footprint_ref = fp_prop.value if fp_prop else ''
    fp_filters_prop = props.get('ki_fp_filters')
    is_generic = not footprint_ref and bool(fp_filters_prop and fp_filters_prop.value)

    names_hidden = symbol.pinNamesHide
    numbers_hidden = symbol.hidePinNumbers

    gate_info = []
    for letter, bodies in _group_gates(symbol):
        sym_name = symbol.libId if letter is None else f'{symbol.libId}_{letter}'
        sym_el, named_pins = _build_gate_symbol(sym_name, bodies, names_hidden, numbers_hidden)

        for p in symbol.properties:
            if _is_hidden(p) or p.key in _KICAD_RESERVED_PROP_KEYS:
                continue
            if p.key == 'Reference':
                placeholder = 'NAME'
            elif p.key == 'Value':
                placeholder = 'VALUE'
            else:
                placeholder = clean_attr_name(p.key)
            if placeholder:
                _emit_property_placeholder(sym_el, placeholder, p)

        if sym_name not in pool:
            symbols_el.append(sym_el)
            pool[sym_name] = sym_el
        gate_info.append((letter, sym_name, named_pins))

    if symbol.isPower and not _is_erc_power_source(symbol):
        # #PWR power-flag symbol (KiCad Library Convention S7.1, confirmed
        # both via klc.kicad.org and real data, testData/phil/symlib.kicad_sym):
        # Reference="#PWR", exactly one Power Input pin (own name always
        # blank), Value names both the symbol and the net it globally joins.
        # Eagle's equivalent is a supply-symbol DEVICE with no package — IR
        # already models this as a `sup` pin (ir_schema.md "Supply symbol"):
        # exactly one per symbol, net name AND displayed value taken from
        # the pin's OWN name alone. KiCad splits that across two facts
        # (Value property vs. the pin's perpetually-blank name) — collapse
        # them into IR's single source here, at the import boundary.
        value_prop = props.get('Value')
        pin_el = pool[sym_name].find('pin')
        if pin_el is not None and value_prop and value_prop.value:
            pin_el.set('name', clean_attr_name(_kicad_overbar_to_eagle(value_prop.value)))
            pin_el.set('direction', 'sup')

    attrs = []
    for p in symbol.properties:
        if p.key == 'Reference':
            continue
        key = clean_attr_name(p.key).lower()
        if key:
            attrs.append((key, '' if _kicad_blank(p.value) else _kicad_overbar_to_eagle(p.value)))

    return {
        'prefix': prefix,
        'is_generic': is_generic,
        'footprint_ref': footprint_ref,
        'attrs': attrs,
        'gates': gate_info,
    }


# ---------------------------------------------------------------------------
# Footprint -> IR
# ---------------------------------------------------------------------------

def _rot_fp(angle):
    """Footprint-space rotation -> IR: NO negation, angle passes through.

    Was `(-angle) % 360` — the same invert-the-angle formula already
    disproven for schematic symbols (_mirror_and_angle) and schematic
    labels; DISPROVEN for footprint space too by visual ground truth
    (testData/rtfp, pad rotated 30 deg in real KiCad rendered mirrored in
    IR — see decisions.md "KiCad: _rot_fp"). The Y-flip of coordinates does
    NOT flip the rotation sign: KiCad's angle convention is already
    CCW-on-screen, same as IR's. kicad_exporter's footprint side
    (_fp_text_style / fp_text / pad) is the matching no-negation inverse —
    both sides changed together, KiCad round-trip stays green.

    Used for ALL footprint-space angles (pads, texts, >NAME/>VALUE
    placeholders alike — the former _rot_fp_placeholder split existed only
    to mirror an export-side asymmetry that is now gone).
    """
    return _f((angle or 0) % 360)


def _smd_shape_exact(pad):
    """True if `pad.shape` maps onto IR <smd>'s (rounded-rect/circle) model
    exactly, no fallback needed. `roundrect` WITH chamfered corners
    (`chamfer`/`chamferRatio` — KiCad's "Chamfered rectangle"/"Chamfered with
    other corners rounded" Pad Shape options, see decisions.md "KiCad: формы
    падов") is NOT exact — same file-format `shape` value as a clean
    roundrect, but the chamfer itself has no IR equivalent and would
    otherwise be silently dropped with no signal at all.
    """
    if pad.shape in ('circle', 'oval', 'rect'):
        return True
    if pad.shape == 'roundrect':
        return not pad.chamfer
    return False


def _smd_roundness(pad):
    """Pad shape -> IR <smd> roundness (0-100). IR <smd> only models a
    (rounded) rectangle (see kicad_exporter's smd branch) — round/oval pads
    become roundness=100 (max rounding ≈ circular, same convention used for
    PCB-circle fiducials elsewhere in this project); anything _smd_shape_exact
    rejects (trapezoid/custom/chamfered) falls back to a plain rect,
    roundness=0 — caller is responsible for logging that fallback.
    """
    if pad.shape in ('circle', 'oval'):
        return 100
    if pad.shape == 'roundrect' and not pad.chamfer and pad.roundrectRatio:
        return round(pad.roundrectRatio * 200)  # ratio is vs. min(w,h)/2; IR is vs. min(w,h)
    return 0


def _pad_shape(pad):
    """Pad shape -> IR <pad>/<hole> shape ('round'/'square' only)."""
    return _PAD_SHAPE_FALLBACK.get(pad.shape, 'round')


def _convert_footprint(fp, fp_name, models_dir=None, out_models_dir=None):
    """One kiutils Footprint -> IR <footprint name=...>.

    Footprint space is Y-down in KiCad, Y-up in IR — every coordinate is
    flipped (inverse of kicad_exporter.export_footprint's `_ky`), unlike the
    symbol side which needs no flip (see _add_unit_geometry).

    `placement_angle`: when `fp` is a footprint placed on a board (project
    import — kicad_project_parser.py reads geometry straight off the board,
    not via fp-lib-table, see its module docstring for why), KiCad bakes the
    board placement rotation directly into every child pad/text's own stored
    angle — confirmed empirically (testData/kicad9-ti-mspm0-tutorial,
    R_0603_1608Metric placed at 4 different board rotations: stored pad
    angle exactly equals the placement angle every time, all of them
    resolving to the SAME true local angle once subtracted). Position
    (x/y) is NOT similarly combined — only angle. A standalone library
    `.kicad_mod` (no board placement context) has `fp.position is None`, so
    this is always a harmless no-op for the library-only import path.
    """
    placement_angle = (fp.position.angle or 0) if fp.position else 0
    fp_el = ET.Element('footprint', name=fp_name)
    if fp.description:
        d = ET.SubElement(fp_el, 'description')
        d.text = fp.description

    def _layer_n(kicad_layer):
        """KiCad layer name -> IR layer attribute value (str) or None (drop).
        Geometry is a DIRECT child of <footprint> carrying layer="N" — no
        per-layer container tags (unified with the board layer model,
        ir_schema.md "Плата (Board IR)")."""
        n = _FP_LAYER_TO_IR.get(kicad_layer)
        return None if n is None else str(n)

    for item in fp.graphicItems:
        tag = type(item).__name__

        if tag == 'FpText':
            if item.hide:
                continue
            ln = _layer_n(item.layer)
            if ln is None:
                continue
            if item.type == 'reference':
                t = ET.SubElement(fp_el, 'text', x=_um(item.position.X), y=_um(-item.position.Y),
                                   size=_text_size_um(item.effects),
                                   rot=_rot_fp((item.position.angle or 0) - placement_angle),
                                   align=_align(item.effects.justify if item.effects else None),
                                   layer=ln)
                t.text = '>NAME'
            elif item.type == 'value':
                t = ET.SubElement(fp_el, 'text', x=_um(item.position.X), y=_um(-item.position.Y),
                                   size=_text_size_um(item.effects),
                                   rot=_rot_fp((item.position.angle or 0) - placement_angle),
                                   align=_align(item.effects.justify if item.effects else None),
                                   layer=ln)
                t.text = '>VALUE'
            elif item.type == 'user' and (item.text or '').strip().upper() in ('${REFERENCE}', '${VALUE}'):
                # KiCad text variable on a plain decorative `fp_text user`
                # item — a separate copy of the designator/value (often on
                # a documentation layer like F.Fab), distinct from the
                # dedicated `type='reference'`/`'value'` item AND from the
                # modern `(property "Reference"...)` token
                # (`_footprint_name_value_geometry`) — all three can
                # coexist on one footprint (confirmed real: testData/
                # vimdrones.zip's HVSON-8-1EP has a real `Reference`
                # property AND this `fp_text user "${REFERENCE}"` on
                # F.Fab). Same `_rot_fp` as any other plain decorative
                # text below (since the rtfp ground-truth fix _rot_fp is
                # the single formula for every footprint-space angle,
                # placeholder or not). Only the literal text changes
                # (-> IR placeholder, not the raw `${...}` string), nothing
                # about position/rotation handling.
                t = ET.SubElement(fp_el, 'text', x=_um(item.position.X), y=_um(-item.position.Y),
                                   size=_text_size_um(item.effects),
                                   rot=_rot_fp((item.position.angle or 0) - placement_angle),
                                   align=_align(item.effects.justify if item.effects else None),
                                   layer=ln)
                t.text = '>NAME' if '${REFERENCE}' in item.text.upper() else '>VALUE'
            else:
                t = ET.SubElement(fp_el, 'text', x=_um(item.position.X), y=_um(-item.position.Y),
                                   size=_text_size_um(item.effects),
                                   rot=_rot_fp((item.position.angle or 0) - placement_angle),
                                   align=_align(item.effects.justify if item.effects else None),
                                   layer=ln)
                t.text = _kicad_overbar_to_eagle(_kicad_multiline_to_eagle(item.text))
            continue

        ln = _layer_n(item.layer)
        if ln is None:
            continue

        if tag == 'FpLine':
            ET.SubElement(fp_el, 'line',
                          x1=_um(item.start.X), y1=_um(-item.start.Y),
                          x2=_um(item.end.X), y2=_um(-item.end.Y),
                          width=_stroke_width_um(item), layer=ln)

        elif tag == 'FpRect':
            x1, y1, x2, y2 = item.start.X, -item.start.Y, item.end.X, -item.end.Y
            w = _stroke_width_um(item)
            for ax1, ay1, ax2, ay2 in [(x1, y1, x2, y1), (x2, y1, x2, y2),
                                        (x2, y2, x1, y2), (x1, y2, x1, y1)]:
                ET.SubElement(fp_el, 'line', x1=_um(ax1), y1=_um(ay1), x2=_um(ax2), y2=_um(ay2),
                              width=w, layer=ln)

        elif tag == 'FpCircle':
            r = math.hypot(item.end.X - item.center.X, item.end.Y - item.center.Y)
            ET.SubElement(fp_el, 'arc', cx=_um(item.center.X), cy=_um(-item.center.Y), r=_um(r),
                          start='0', sweep='360', width=_stroke_width_um(item), layer=ln)

        elif tag == 'FpArc':
            params = _arc_params((item.start.X, -item.start.Y),
                                  (item.mid.X, -item.mid.Y),
                                  (item.end.X, -item.end.Y))
            if params is None:
                continue
            cx, cy, r, start, sweep = params
            # _um(), not _f(): cx/cy/r are mm from _arc_params — same
            # mislabeled-as-µm bug as the symbol-side SyArc branch (see
            # decisions.md "KiCad-специфика: filled дуга → круг"), just on
            # the footprint/PCB-arc side. Not exercised by any current test
            # fixture (no footprint here has a real partial arc), found by
            # inspection while chasing the Eagle-export arc-orientation bug.
            ET.SubElement(fp_el, 'arc', cx=_um(cx), cy=_um(cy), r=_um(r),
                          start=_f(start), sweep=_f(sweep), width=_stroke_width_um(item), layer=ln)

        elif tag == 'FpPoly':
            # item.fill is None when the token is absent entirely — kiutils'
            # own docstring: "If not defined, the [polygon] is not filled",
            # so the correct default is 0 (outline only), NOT the IR
            # attribute's own documented default (100) — that default is
            # for readers of a hand-written IR file missing the attribute,
            # not for a source that explicitly has no fill token at all.
            fill_pct = _FP_FILL_PERCENT.get(item.fill, 0)
            poly = ET.SubElement(fp_el, 'polygon', width=_stroke_width_um(item),
                                  fill=str(fill_pct), layer=ln)
            for pt in item.coordinates:
                ET.SubElement(poly, 'vertex', x=_um(pt.X), y=_um(-pt.Y))

    # Modern footprints (KiCad 9/10) carry Reference/Value as footprint-level
    # `(property ...)` tokens with their own real position/layer/effects —
    # NOT as the old dedicated FpText(type='reference'/'value') handled
    # above (confirmed: testData/phil.zip's SOT-23-5 has no such FpText at
    # all, just a decorative FpText(type='user', text='${REFERENCE}') with
    # no usable position — see _footprint_name_value_geometry). Per the
    # user: only the GEOMETRY matters here, never the property's actual
    # text content — it becomes the same >NAME/>VALUE placeholder the old
    # FpText path produces, not real text.
    for key, placeholder in (('Reference', 'NAME'), ('Value', 'VALUE')):
        entry = getattr(fp, '_name_value_geometry', {}).get(key)
        if entry is None:
            continue
        position, effects, layer, hide = entry
        if hide:
            continue
        ln = _layer_n(layer)
        if ln is None:
            continue
        t = ET.SubElement(fp_el, 'text', x=_um(position.X), y=_um(-position.Y),
                           size=_text_size_um(effects),
                           rot=_rot_fp((position.angle or 0) - placement_angle),
                           align=_align(effects.justify if effects else None),
                           layer=ln)
        t.text = f'>{placeholder}'

    # KiCad allows several physical pads to share one pad NUMBER (e.g. several
    # mounting/ground pads all logically "pad 2") — legal there, but Eagle/
    # Altium require unique designators within a footprint. Disambiguate with
    # an a/b/c suffix on the IR side, and remember the grouping so the pin
    # that targets this pad number can still list all of them — same
    # convention ir_schema.md already uses for thermal/exposed-pad multi-pad
    # pins (`<map pad="4 9" pin="..."/>`), just synthesized here instead of
    # coming from the source format directly.
    #
    # Lowercase here is fine — IR itself has no case restriction on
    # designators. Eagle specifically rejects lowercase pad designators
    # (confirmed against real Eagle, testData/test.lbr — see
    # eagle_exporter.py), but that's an Eagle-export-time concern, not an IR
    # one; eagle_exporter uppercases pad names/references on its own side,
    # same as it already does for attribute names. Don't bake any one
    # target's case rules into the parser that builds the IR.
    #
    # An EMPTY number (confirmed on testData/video/sim72.kicad_mod — 3 large
    # mechanical thru_hole pads with no number, flanking the connector's 72
    # real numbered pins) is NOT the same kind of duplicate: it doesn't mean
    # "these share a signal" the way a repeated real number does — each blank
    # pad is independently unconnected, grouping them would invent a signal
    # that isn't there. So they're never merged into one group; each gets its
    # own synthetic, obviously-not-a-real-designator name instead.
    pad_name_groups = {}
    _blank_pad_n = 0

    for pad in fp.pads:
        x, y = _um(pad.position.X), _um(-pad.position.Y)
        rot = _rot_fp((pad.position.angle or 0) - placement_angle)

        # No copper layer at all (e.g. only `F.Paste`/`F.Mask`) -> KiCad's
        # "SMD Aperture" pad type (no dedicated `pad.type` value of its own —
        # same file-format `smd` as a real pad, distinguished purely by its
        # layer set having no `.Cu`) — not an electrical contact, just
        # stencil/mask geometry (confirmed real: testData/vimdrones.zip's
        # *-1EP QFN/DFN/HVSON exposed-pad footprints — genuine `F.Cu`+
        # `F.Mask` pad carrying `pad_prop_heatsink`, plus several small
        # blank-number `F.Paste`-only fragments inside its bounds, so the
        # stencil doesn't print one solid blob). Imported as plain `<shape>`
        # geometry on whichever paste/mask layer number (±131/±129) its
        # layer set actually selects — one shape per
        # selected layer, not a `<smd>`/`<pad>` (no pin-mapping entry, no
        # name needed). No technical layer at all selected -> nothing to
        # draw, dropped with a log line (ir_schema.md "KiCad: технические
        # слои smd-пада").
        if not any(l.endswith('.Cu') for l in pad.layers):
            aperture_layers = [ir for kicad_l, ir in
                                (('F.Paste', '131'), ('B.Paste', '-131'),
                                 ('*.Paste', '131'),
                                 ('F.Mask', '129'), ('B.Mask', '-129'),
                                 ('*.Mask', '129'))
                                if kicad_l in pad.layers]
            if not aperture_layers:
                import_log.log(fp_name, pad.number or '(blank)',
                                'APERTURE_NO_LAYER dropped, layers=', list(pad.layers))
                continue
            shape_exact = _smd_shape_exact(pad)
            if not shape_exact:
                import_log.log(fp_name, pad.number or '(blank)', f'PAD_SHAPE {pad.shape} ->', 'rectangular')
            roundness = 0 if not shape_exact else _smd_roundness(pad)
            for ir_layer in aperture_layers:
                ET.SubElement(fp_el, 'shape', x=x, y=y,
                              w=_um(pad.size.X), h=_um(pad.size.Y),
                              roundness=str(roundness), outline='0', rot=rot,
                              layer=ir_layer)
            continue

        tag = _PAD_TAG.get(pad.type, 'smd')
        raw_name = _kicad_overbar_to_eagle(pad.number)
        if raw_name:
            dup_n = len(pad_name_groups.get(raw_name, []))
            name = raw_name if dup_n == 0 else f'{raw_name}{chr(ord("a") + dup_n - 1)}'
            pad_name_groups.setdefault(raw_name, []).append(name)
        else:
            _blank_pad_n += 1
            name = f'_NC{_blank_pad_n}'

        if tag == 'smd':
            # <smd> carries no layer (mount-side copper by construction);
            # the rare far-side pad (B.Cu-only in a library footprint) keeps
            # its sidedness via an explicit layer="-1".
            far_side = 'B.Cu' in pad.layers and 'F.Cu' not in pad.layers
            shape_exact = _smd_shape_exact(pad)
            if not shape_exact:
                import_log.log(fp_name, name, f'PAD_SHAPE {pad.shape} ->', 'rectangular')
            # ir_schema.md "KiCad: технические слои smd-пада" — `paste`/
            # `stopmask` are read from this pad's OWN technical-layer
            # selection, default 0 (not IR's own default 1) when the layer
            # is plainly absent from the list — KiCad expresses intent via
            # layer presence, not a separate yes/no property.
            has_paste = any(l in pad.layers for l in ('F.Paste', 'B.Paste', '*.Paste'))
            has_mask = any(l in pad.layers for l in ('F.Mask', 'B.Mask', '*.Mask'))
            smd_el = ET.SubElement(fp_el, 'smd', name=name, x=x, y=y,
                          width=_um(pad.size.X), height=_um(pad.size.Y),
                          roundness=str(_smd_roundness(pad) if shape_exact else 0), rot=rot)
            if far_side:
                smd_el.set('layer', '-1')
            if not has_paste:
                smd_el.set('paste', '0')
            if not has_mask:
                smd_el.set('stopmask', '0')

        elif tag == 'pad':
            drill = pad.drill
            if drill and drill.oval:
                import_log.log(fp_name, name, 'DRILL_SHAPE oval ->', f'round(diameter={drill.diameter}mm)')
            diameter = drill.diameter if drill else pad.size.X
            has_mask = any(l in pad.layers for l in ('F.Mask', 'B.Mask', '*.Mask'))
            pad_el = ET.SubElement(fp_el, 'pad', name=name, x=x, y=y,
                          drill=_um(diameter), shape=_pad_shape(pad))
            if not has_mask:
                pad_el.set('stopmask', '0')

        elif tag == 'hole':
            drill = pad.drill
            diameter = drill.diameter if drill else pad.size.X
            ET.SubElement(fp_el, 'hole', x=x, y=y, drill=_um(diameter))

    if fp.models:
        model = fp.models[0]
        m3 = ET.SubElement(fp_el, 'model3d')
        m3.set('tx', _um(model.pos.X)); m3.set('ty', _um(-model.pos.Y)); m3.set('tz', _um(model.pos.Z))
        m3.set('rx', _f(-model.rotate.X)); m3.set('ry', _f(model.rotate.Y)); m3.set('rz', _f(-model.rotate.Z))
        if models_dir is not None and out_models_dir is not None:
            from babel.ir_util import resolve_model3d_file
            src = resolve_model3d_file(fp_el, models_dir)
            if src is not None:
                out_models_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(src, out_models_dir / src.name)

    return fp_el, pad_name_groups


# ---------------------------------------------------------------------------
# Footprint resolution via fp-lib-table
# ---------------------------------------------------------------------------

def _resolve_footprint_path(footprint_ref, project_dir, lib_table):
    """'Nickname:Name' -> Path to the .kicad_mod, or None.

    Tries the registered nickname's library first. Real-world project-exported
    libraries (see project_kicad_real_world_libs memory / progress.md) often
    carry stale nicknames from wherever the symbols were originally sourced —
    the registered fp-lib-table may not even have that nickname — so this
    falls back to searching every registered library for the bare footprint
    name if the nickname lookup doesn't pan out.
    """
    if not footprint_ref or ':' not in footprint_ref or lib_table is None:
        return None
    nickname, name = footprint_ref.split(':', 1)

    def _lib_dir(lib):
        return Path(lib.uri.replace('${KIPRJMOD}', str(project_dir)))

    for lib in lib_table.libs:
        if lib.name == nickname:
            p = _lib_dir(lib) / f'{name}.kicad_mod'
            if p.exists():
                return p
            break

    for lib in lib_table.libs:
        p = _lib_dir(lib) / f'{name}.kicad_mod'
        if p.exists():
            return p
    return None


def _resolve_symbol(lib_id, project_dir, lib_table, lib_cache):
    """'Nickname:Name' -> kiutils Symbol (read fresh from the real library),
    or None. Mirrors _resolve_footprint_path's nickname-first-then-fallback
    search — confirmed needed on a real re-exported project (testData/
    phil.zip): the user's own manual "Export symbols/footprints to new
    library" workflow does NOT relink the schematic's own `lib_id`
    references to the new nickname (still says e.g. "Device:C", even though
    sym-lib-table only registers "symlib" -> symlib.kicad_sym) — so nickname
    matching alone would resolve nothing at all.

    A sym-lib-table nickname points to ONE .kicad_sym file that may contain
    many symbols (unlike fp-lib-table, where each footprint is its own
    file), so each candidate library is loaded (via _read_symbol_lib, which
    already carries its own Property-hide workaround) and searched by bare
    entryName. lib_cache: {file_path: SymbolLib | None}, shared across calls
    so the same .kicad_sym isn't re-parsed once per symbol.
    """
    if not lib_id or ':' not in lib_id or lib_table is None:
        return None
    nickname, name = lib_id.split(':', 1)

    def _lib_path(lib):
        return Path(lib.uri.replace('${KIPRJMOD}', str(project_dir)))

    ordered = [lib for lib in lib_table.libs if lib.name == nickname]
    ordered += [lib for lib in lib_table.libs if lib.name != nickname]
    for lib in ordered:
        p = _lib_path(lib)
        if p not in lib_cache:
            try:
                lib_cache[p] = _read_symbol_lib(p) if p.exists() else None
            except Exception:
                lib_cache[p] = None
        symlib = lib_cache[p]
        if symlib is None:
            continue
        for sym in symlib.symbols:
            if sym.entryName == name:
                return sym
    return None


# ---------------------------------------------------------------------------
# Component assembly
# ---------------------------------------------------------------------------

def _pin_numbers(pin):
    """Every footprint pad NUMBER one symbol pin maps to. Almost always a
    single value (`pin.number`) — except KiCad's two "one symbol pin, many
    pads" representations (no direct concept of this exists in KiCad
    itself, see decisions.md "Stacked pins"):
      - KiCad 10 "stacked pins": `pin.number` is itself a bracketed,
        comma-separated list, `"[A1,A12,B1,B12]"`.
      - Older libraries: several sibling <pin> elements stacked on one
        coordinate, collapsed by _collapse_stacked_pins before this point —
        the survivor's `_stacked_numbers` carries every sibling's plain
        number (each expanded through the same bracket check, in case a
        collapsed group ever mixes the two forms).
    """
    raw_numbers = getattr(pin, '_stacked_numbers', None) or [pin.number]
    out = []
    for raw in raw_numbers:
        raw = raw or ''
        if raw.startswith('[') and raw.endswith(']'):
            out += [p.strip() for p in raw[1:-1].split(',') if p.strip()]
        else:
            out.append(raw)
    return out


def _pin_mapping(named_pins, pad_name_groups):
    """[(ir_pin_name, 'pad pad...'), ...] for pins whose number(s) match a
    real pad on the footprint (symbol pin NUMBER <-> footprint pad NUMBER is
    the only correspondence KiCad gives us — same convention Eagle/Altium
    use). See _pin_numbers for why one symbol pin can carry more than one.

    pad_name_groups (from _convert_footprint) maps the original pad NUMBER to
    every disambiguated IR pad name sharing it — usually just one, but KiCad
    allows several physical pads under one logical number (e.g. several
    ground/mounting pads all "pad 2"); all of them are joined space-separated
    in a single <map>, same convention as the documented thermal/exposed-pad
    multi-pad-per-pin case.
    """
    out = []
    for ir_name, pin in named_pins:
        names = []
        for num in _pin_numbers(pin):
            names += pad_name_groups.get(num, [])
        if names:
            out.append((ir_name, ' '.join(names)))
    return out


def convert(sym_path, output_path, project_dir=None):
    sym_path = Path(sym_path)
    output_path = Path(output_path)
    project_dir = Path(project_dir) if project_dir else sym_path.parent

    lib = _read_symbol_lib(sym_path)

    lib_table = None
    fp_lib_table_path = project_dir / 'fp-lib-table'
    if fp_lib_table_path.exists():
        lib_table = LibTable.from_file(str(fp_lib_table_path), encoding='utf-8')

    lib_el = ET.Element('library', name=sym_path.stem)
    symbols_el = ET.SubElement(lib_el, 'symbols')
    pool = {}

    # Bare-stem sidecar dir, matching the IR canon (ir_schema.md "Соглашение о
    # расположении файлов моделей") that altium_exporter/eagle reuse — NOT the
    # `.3dshapes` suffix, which is KiCad's own native sibling-of-.pretty naming,
    # only correct on the *output* side (kicad_exporter, writing a real
    # KiCad-readable library), not here where we're producing a fresh .swlib.
    out_models_dir = output_path.parent / output_path.stem
    fp_cache = {}   # resolved Path -> <footprint> Element (dedup shared footprints)
    n_components = 0

    for symbol in lib.symbols:
        info = _convert_symbol(symbol, pool, symbols_el)
        if info is None:
            continue   # derived (extends) symbol — not yet supported
        if info['is_generic']:
            # Footprint empty + ki_fp_filters: KiCad itself doesn't pair a
            # generic symbol with a real footprint until it's placed on a
            # schematic (per-instance) — the library carries no such pair.
            # Orphan in the pool, same as Eagle's symbol-only ZD/M03.
            continue

        comp_el = ET.Element('component', name=symbol.entryName, prefix=info['prefix'])

        fp_el = None
        pad_name_groups = None
        if info['footprint_ref']:
            fp_path = _resolve_footprint_path(info['footprint_ref'], project_dir, lib_table)
            if fp_path is not None:
                if fp_path not in fp_cache:
                    fp = _read_footprint(fp_path)
                    models_dir = fp_path.parent.parent / f'{fp_path.parent.stem}.3dshapes'
                    fp_cache[fp_path] = _convert_footprint(
                        fp, fp.entryName, models_dir=models_dir, out_models_dir=out_models_dir)
                fp_el, pad_name_groups = fp_cache[fp_path]
            else:
                print(f'  ! {symbol.libId}: footprint "{info["footprint_ref"]}" not found '
                      f'in fp-lib-table (or fallback by name)')

        gates = info['gates']
        if len(gates) == 1 and gates[0][0] is None:
            comp_el.set('symbol', gates[0][1])
            _, _, named_pins = gates[0]
            all_named_pins = [named_pins]
        else:
            for letter, sym_name, _ in gates:
                ET.SubElement(comp_el, 'gate', name=letter, symbol=sym_name)
            all_named_pins = [np for _, _, np in gates]

        if fp_el is not None:
            # fp_el may be shared (fp_cache, several components using the
            # same footprint) — deep-copy children, ElementTree elements can
            # only have one parent and a second append() would silently move
            # them out of the first component's footprint.
            comp_fp = ET.SubElement(comp_el, 'footprint', name=fp_el.get('name'))
            for child in fp_el:
                comp_fp.append(copy.deepcopy(child))
            pm_el = ET.SubElement(comp_fp, 'pin-mapping')
            if len(gates) == 1 and gates[0][0] is None:
                for ir_name, pad_num in _pin_mapping(all_named_pins[0], pad_name_groups):
                    ET.SubElement(pm_el, 'map', pin=ir_name, pad=pad_num)
            else:
                for (letter, _, named_pins) in gates:
                    for ir_name, pad_num in _pin_mapping(named_pins, pad_name_groups):
                        ET.SubElement(pm_el, 'map', pin=f'{letter}.{ir_name}', pad=pad_num)

        if info['attrs']:
            attrs_el = ET.SubElement(comp_el, 'attributes')
            for k, v in info['attrs']:
                ET.SubElement(attrs_el, 'attr', name=k, value=v)

        lib_el.append(comp_el)
        n_components += 1

    raw = minidom.parseString(ET.tostring(lib_el, encoding='unicode')).toprettyxml(indent='  ')
    output_path.write_text(raw, encoding='utf-8')
    print(f'Written: {output_path}  ({n_components} components)')
    import_log.write(output_path)
    return str(output_path)


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'testData/multichannel/multichannel_mixer.kicad_sym'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'testData/multichannel.swlib'
    convert(src, dst)
